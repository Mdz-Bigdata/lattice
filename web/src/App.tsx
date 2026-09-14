// SPDX-License-Identifier: Apache-2.0
import { Component, useEffect, useState, type ReactNode } from "react";
import {
  Activity,
  ArrowDownToLine,
  BookOpen,
  Boxes,
  ChartNoAxesCombined,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Database,
  Expand,
  Globe2,
  Layers,
  Menu,
  MessageSquareText,
  Network,
  RefreshCw,
  Search,
  ShieldCheck,
  TerminalSquare,
  Users,
  X,
} from "lucide-react";
import {
  request,
  errorMessage,
  LOCAL_SAMPLE_ID,
  type Bootstrap,
  type DataSource,
  type Health,
  type QueryResult,
  type SourceType,
} from "./api";
import { ErrorBanner, Loading } from "./components";
import QueryPage from "./QueryPage";
import {
  IngestionPage,
  MapPage,
  OverviewPage,
  QualityPage,
  SemanticPage,
  ServicesPage,
  SourcesPage,
  SqlPage,
  StandardsPage,
} from "./DataPages";
import { PolarisExplorer, PolarisOverviewPage } from "./PolarisPages";

const navigation = [
  { id: "overview", label: "指标总览", icon: ChartNoAxesCombined },
  { id: "map", label: "数据地图", icon: Globe2 },
  { id: "sources", label: "数据源", icon: Database },
  { id: "ingestion", label: "数据接入", icon: ArrowDownToLine },
  { id: "quality", label: "数据质量", icon: ShieldCheck },
  { id: "standards", label: "标准规范", icon: BookOpen },
  { id: "semantic", label: "语义模型", icon: Network },
  { id: "sql", label: "SQL 工作台", icon: TerminalSquare },
  { id: "services", label: "数据服务", icon: Boxes },
  { id: "questions", label: "智能问数", icon: MessageSquareText },
  {
    id: "catalogs",
    label: "Catalog 管理",
    icon: Layers,
    group: "Apache Polaris",
  },
  { id: "identities", label: "身份与权限", icon: Users },
  { id: "explorer", label: "API 控制台", icon: TerminalSquare },
];
function initialPage() {
  const id = window.location.hash.slice(1);
  return navigation.some((item) => item.id === id) ? id : "questions";
}

/** How often an idle tab re-checks the build; returning to it also checks. */
const BUILD_CHECK_MS = 5 * 60 * 1000;

export default function App() {
  const [page, setPage] = useState(initialPage);
  const [tabs, setTabs] = useState<string[]>(() => [
    ...new Set(["overview", initialPage()]),
  ]);
  const [sidebarOpen, setSidebarOpen] = useState(() => window.innerWidth > 860);
  const [searchOpen, setSearchOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [bootstrap, setBootstrap] = useState<Bootstrap | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [error, setError] = useState("");
  const [result, setResult] = useState<QueryResult | null>(null);
  const [sql, setSql] = useState("");
  const [explorerSearch, setExplorerSearch] = useState("");
  const [revision, setRevision] = useState(0);
  const [fullscreenError, setFullscreenError] = useState("");
  const [stale, setStale] = useState(false);
  const [sources, setSources] = useState<DataSource[]>([]);
  const [sourceTypes, setSourceTypes] = useState<SourceType[]>([]);
  const [sourcesError, setSourcesError] = useState("");
  const [sourceId, setSourceId] = useState(LOCAL_SAMPLE_ID);
  async function reloadSources() {
    try {
      const data = await request<{ items: DataSource[]; types: SourceType[] }>(
        "/api/datasources",
      );
      setSources(data.items);
      setSourceTypes(data.types);
      setSourcesError("");
      setSourceId((current) =>
        data.items.some((item) => item.id === current)
          ? current
          : ((
              data.items.find((item) => item.id === LOCAL_SAMPLE_ID) ??
              data.items[0]
            )?.id ?? current),
      );
    } catch (e) {
      setSourcesError(errorMessage(e));
    }
  }
  useEffect(() => {
    request<Bootstrap>("/api/bootstrap")
      .then(setBootstrap)
      .catch((e) => setError(errorMessage(e)));
    void reloadSources();
    const onHashChange = () => {
      const id = initialPage();
      setPage(id);
      setTabs((current) => (current.includes(id) ? current : [...current, id]));
    };
    window.addEventListener("hashchange", onHashChange);
    // This tab keeps running the bundle it loaded, so a rebuilt WebUI would
    // otherwise stay invisible here until someone happened to reload.  Compare
    // the build behind the API with the one this tab started on, when the tab
    // is looked at again, and periodically while it is left open.
    let loaded = "";
    const checkBuild = async () => {
      try {
        const status = await request<Health>("/api/health");
        setHealth(status);
        if (!status.build) return;
        if (!loaded) loaded = status.build;
        else if (status.build !== loaded) setStale(true);
      } catch {
        // A failed check says nothing about the build; the next one retries.
      }
    };
    // The first read also loads the health panel, so it runs even in a tab that
    // opened in the background; only the re-checks wait for the tab to be seen.
    void checkBuild();
    const recheck = () => {
      if (!document.hidden) void checkBuild();
    };
    const timer = window.setInterval(recheck, BUILD_CHECK_MS);
    document.addEventListener("visibilitychange", recheck);
    window.addEventListener("focus", recheck);
    return () => {
      window.removeEventListener("hashchange", onHashChange);
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", recheck);
      window.removeEventListener("focus", recheck);
    };
  }, []);
  function goTo(id: string, query?: string, source?: string) {
    if (source) setSourceId(source);
    if (query !== undefined) {
      setSql(query);
      setRevision((value) => value + 1);
    }
    setPage(id);
    window.location.hash = id;
    setTabs((current) => (current.includes(id) ? current : [...current, id]));
    setSearchOpen(false);
    setSearch("");
    if (window.innerWidth <= 860) setSidebarOpen(false);
  }
  function explore(query: string) {
    setExplorerSearch(query);
    setRevision((value) => value + 1);
    goTo("explorer");
  }
  function closeTab(id: string) {
    const remaining = tabs.filter((tab) => tab !== id);
    setTabs(remaining.length ? remaining : ["overview"]);
    if (page === id) {
      const next = remaining.at(-1) ?? "overview";
      setPage(next);
      window.location.hash = next;
    }
  }
  async function fullscreen() {
    try {
      if (document.fullscreenElement) await document.exitFullscreen();
      else await document.documentElement.requestFullscreen();
      setFullscreenError("");
    } catch {
      setFullscreenError("当前浏览器暂不支持全屏，可使用浏览器的全屏菜单。");
    }
  }
  const label =
    navigation.find((item) => item.id === page)?.label ?? "智能问数";
  const shared = bootstrap
    ? {
        bootstrap,
        health,
        goTo,
        explore,
        sources,
        sourceTypes,
        sourcesError,
        sourceId,
        setSourceId,
        reloadSources,
      }
    : null;
  const views: Record<string, () => ReactNode> = shared
    ? {
        overview: () => <OverviewPage {...shared} />,
        map: () => <MapPage {...shared} />,
        sources: () => <SourcesPage {...shared} />,
        ingestion: () => <IngestionPage {...shared} />,
        quality: () => <QualityPage {...shared} />,
        standards: () => <StandardsPage {...shared} />,
        semantic: () => <SemanticPage {...shared} />,
        sql: () => <SqlPage {...shared} initialSql={sql} />,
        services: () => <ServicesPage {...shared} />,
      }
    : {};
  return (
    <div
      className={`app-shell ${sidebarOpen ? "sidebar-open" : "sidebar-closed"}`}
    >
      {stale && (
        <div className="stale-build" role="status">
          <RefreshCw size={13} />
          <span>页面版本已更新，刷新后可使用最新功能。</span>
          <button onClick={() => window.location.reload()}>立即刷新</button>
        </div>
      )}
      {sidebarOpen && (
        <button
          className="sidebar-overlay"
          aria-label="关闭菜单"
          onClick={() => setSidebarOpen(false)}
        />
      )}
      <aside className="sidebar">
        <a
          className="brand"
          href="#questions"
          onClick={() => goTo("questions")}
          aria-label="Lattice 数据平台首页"
        >
          <svg
            width="33"
            height="34"
            viewBox="0 0 34 36"
            fill="none"
            aria-hidden="true"
          >
            <path
              d="M17 31V5M17 30 3 20M17 27 4 11M17 22 9 4M17 31 31 20M17 27 30 11M17 22 25 4"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinecap="round"
            />
            <path
              d="m17 31-7-16m7 16 7-16"
              stroke="currentColor"
              strokeWidth="1.5"
            />
          </svg>
          <span>Lattice 数据平台</span>
        </a>
        <nav aria-label="主导航">
          {navigation.map((item) => (
            <div key={item.id}>
              {item.group && (
                <div className="navigation-group">{item.group}</div>
              )}
              <button
                className={`nav-item ${page === item.id ? "active" : ""}`}
                onClick={() => goTo(item.id)}
                aria-current={page === item.id ? "page" : undefined}
              >
                <item.icon size={17} strokeWidth={1.65} />
                <span>{item.label}</span>
              </button>
            </div>
          ))}
        </nav>
        <div className="sidebar-foot">
          <i />
          本地工作区<span>LATTICE</span>
        </div>
      </aside>
      <div className="main-shell">
        <header className="topbar">
          <div className="breadcrumb">
            <button
              className="icon-button"
              aria-label={sidebarOpen ? "收起菜单" : "展开菜单"}
              onClick={() => setSidebarOpen(!sidebarOpen)}
            >
              <Menu size={20} />
            </button>
            <button
              className="breadcrumb-home"
              onClick={() => goTo("overview")}
            >
              首页
            </button>
            <span>/</span>
            <span>{label}</span>
          </div>
          <div className="topbar-actions">
            <button
              className="icon-button"
              aria-label="搜索功能"
              onClick={() => setSearchOpen(!searchOpen)}
            >
              <Search size={19} />
            </button>
            <button
              className="icon-button"
              aria-label="切换全屏"
              onClick={() => void fullscreen()}
            >
              <Expand size={17} />
            </button>
            <span className="topbar-divider" />
            <span className="avatar">L</span>
            <span className="user-label">Lattice</span>
          </div>
        </header>
        <div className="tabbar">
          <ChevronLeft className="tab-edge" size={14} />
          <div className="tab-list">
            {tabs.map((id) => (
              <div
                className={`page-tab ${page === id ? "active" : ""}`}
                key={id}
              >
                <button onClick={() => goTo(id)}>
                  {page === id && <i />}
                  {navigation.find((item) => item.id === id)?.label}
                </button>
                {id !== "overview" && (
                  <button
                    className="tab-close"
                    onClick={() => closeTab(id)}
                    aria-label={`关闭${navigation.find((item) => item.id === id)?.label}`}
                  >
                    <X size={11} />
                  </button>
                )}
              </div>
            ))}
          </div>
          <ChevronRight className="tab-edge" size={14} />
          <ChevronDown className="tab-edge" size={14} />
          <button
            className="refresh-button"
            onClick={() => window.location.reload()}
          >
            <RefreshCw size={12} />
            刷新
          </button>
        </div>
        {fullscreenError && <ErrorBanner message={fullscreenError} />}
        {error && <ErrorBanner message={`初始化失败：${error}`} />}
        <main
          className={`main-content ${page === "questions" ? "chat-content" : ""}`}
        >
          <PageBoundary key={`${page}:${revision}`}>
            {page === "questions" ? (
              <QueryPage
                bootstrap={bootstrap}
                health={health}
                result={result}
                setResult={setResult}
                sources={sources}
                sourceId={sourceId}
                setSourceId={setSourceId}
              />
            ) : page === "catalogs" || page === "identities" ? (
              <PolarisOverviewPage
                identities={page === "identities"}
                explore={explore}
              />
            ) : page === "explorer" ? (
              <PolarisExplorer initialSearch={explorerSearch} />
            ) : shared ? (
              views[page]?.()
            ) : (
              !error && <Loading text="正在初始化本地工作区…" />
            )}
          </PageBoundary>
        </main>
      </div>
      {searchOpen && (
        <div className="search-popover">
          <div className="search-popover-input">
            <Search size={16} />
            <input
              aria-label="搜索平台功能"
              placeholder="搜索平台功能…"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Escape") setSearchOpen(false);
              }}
              autoFocus
            />
            <button
              className="icon-button"
              aria-label="关闭搜索"
              onClick={() => setSearchOpen(false)}
            >
              <X size={15} />
            </button>
          </div>
          <div>
            {navigation
              .filter((item) =>
                item.label.toLowerCase().includes(search.toLowerCase()),
              )
              .map((item) => (
                <button key={item.id} onClick={() => goTo(item.id)}>
                  <item.icon size={15} />
                  {item.label}
                  <ChevronRight size={13} />
                </button>
              ))}
          </div>
        </div>
      )}
    </div>
  );
}
class PageBoundary extends Component<
  { children: ReactNode },
  { error: string }
> {
  state = { error: "" };
  static getDerivedStateFromError(error: Error) {
    return { error: error.message };
  }
  render() {
    return this.state.error ? (
      <div className="page-content">
        <ErrorBanner message={`页面加载失败：${this.state.error}`} />
        <button
          className="secondary-button"
          onClick={() => window.location.reload()}
        >
          <Activity size={15} />
          重新加载
        </button>
      </div>
    ) : (
      this.props.children
    );
  }
}
