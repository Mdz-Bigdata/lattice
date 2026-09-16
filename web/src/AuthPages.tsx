// SPDX-License-Identifier: Apache-2.0
/**
 * 多用户与鉴权: the sign-in / first-run setup screen, the top-bar user menu and the
 * 用户与权限 page (accounts, roles, page visibility, sessions and API tokens).
 */
import { useEffect, useState, type FormEvent } from "react";
import { KeyRound, LogOut, Plus, RefreshCw, ShieldCheck, Trash2, UserCircle2, Users } from "lucide-react";
import { errorMessage, request, type AuthStatus, type AuthUser } from "./api";
import { Drawer, Empty, ErrorBanner, Loading, PageTitle } from "./components";

export function LoginPage({ status, onLoggedIn }: { status: AuthStatus; onLoggedIn: (user: AuthUser) => void }) {
  const setup = status.setup_required;
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      const answer = await request<{ user: AuthUser }>(
        setup ? "/api/auth/setup" : "/api/auth/login",
        setup ? { username, password, display_name: displayName || null } : { username, password },
      );
      onLoggedIn(answer.user);
    } catch (e) {
      setError(errorMessage(e));
      setBusy(false);
    }
  }
  return (
    <div className="login-page">
      <form className="login-card" onSubmit={submit}>
        <div className="login-brand">
          <ShieldCheck size={22} />
          <span>Lattice 数据平台</span>
        </div>
        <h1>{setup ? "创建管理员账号" : "登录"}</h1>
        <p className="muted">
          {setup
            ? "这是首次启用登录，请先创建第一个管理员；之后可以在“用户与权限”里添加其他用户。"
            : "请输入用户名和密码。"}
        </p>
        <label htmlFor="login-username">用户名</label>
        <input id="login-username" value={username} autoComplete="username" autoFocus onChange={(event) => setUsername(event.target.value)} />
        {setup && (
          <>
            <label htmlFor="login-display">显示名称</label>
            <input id="login-display" value={displayName} placeholder="可选" onChange={(event) => setDisplayName(event.target.value)} />
          </>
        )}
        <label htmlFor="login-password">密码</label>
        <input
          id="login-password"
          type="password"
          value={password}
          autoComplete={setup ? "new-password" : "current-password"}
          onChange={(event) => setPassword(event.target.value)}
        />
        {setup && <small className="muted">至少 8 个字符。</small>}
        {error && <ErrorBanner message={error} />}
        <button type="submit" className="primary-button" disabled={busy || !username || !password}>
          {busy ? "请稍候…" : setup ? "创建并登录" : "登录"}
        </button>
      </form>
    </div>
  );
}

export function UserMenu({
  user,
  enabled,
  onLoggedOut,
  onOpenUsers,
}: {
  user: AuthUser;
  enabled: boolean;
  onLoggedOut: () => void;
  onOpenUsers: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [password, setPassword] = useState(false);
  useEffect(() => {
    if (!open) return;
    const close = () => setOpen(false);
    window.addEventListener("click", close);
    return () => window.removeEventListener("click", close);
  }, [open]);
  async function logout() {
    try {
      await request("/api/auth/logout", {});
    } finally {
      onLoggedOut();
    }
  }
  return (
    <div className="user-menu" onClick={(event) => event.stopPropagation()}>
      <button type="button" className="user-menu-trigger" onClick={() => setOpen(!open)} aria-haspopup="menu" aria-expanded={open}>
        <span className="avatar">{(user.display_name || user.username).slice(0, 1).toUpperCase()}</span>
        <span className="user-label">{user.display_name || user.username}</span>
      </button>
      {open && (
        <div className="user-menu-panel" role="menu">
          <div className="user-menu-head">
            <b>{user.display_name}</b>
            <small>
              {user.username} · {user.role_label}
            </small>
          </div>
          {user.role === "admin" && (
            <button type="button" role="menuitem" onClick={() => { setOpen(false); onOpenUsers(); }}>
              <Users size={14} />
              用户与权限
            </button>
          )}
          {enabled && (
            <button type="button" role="menuitem" onClick={() => { setOpen(false); setPassword(true); }}>
              <KeyRound size={14} />
              修改密码
            </button>
          )}
          {enabled ? (
            <button type="button" role="menuitem" onClick={() => void logout()}>
              <LogOut size={14} />
              退出登录
            </button>
          ) : (
            <div className="user-menu-note">未启用登录（LATTICE_AUTH=0），当前以本机管理员身份使用。</div>
          )}
        </div>
      )}
      {password && <PasswordDialog onClose={() => setPassword(false)} />}
    </div>
  );
}

function PasswordDialog({ onClose }: { onClose: () => void }) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [again, setAgain] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [done, setDone] = useState("");
  async function save(event: FormEvent) {
    event.preventDefault();
    if (next !== again) return setError("两次输入的新密码不一致。");
    setBusy(true);
    setError("");
    try {
      const outcome = await request<{ revoked_sessions: number }>("/api/auth/password", { current_password: current, new_password: next });
      setDone(`密码已修改，其他 ${outcome.revoked_sessions} 个登录会话已失效。`);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Drawer id="auth-password" title="修改密码" icon={<KeyRound size={17} />} onClose={onClose}>
      <form className="quality-drawer-body" onSubmit={save}>
        <label htmlFor="pw-current">当前密码</label>
        <input id="pw-current" type="password" autoComplete="current-password" value={current} onChange={(event) => setCurrent(event.target.value)} />
        <label htmlFor="pw-next">新密码</label>
        <input id="pw-next" type="password" autoComplete="new-password" value={next} onChange={(event) => setNext(event.target.value)} />
        <label htmlFor="pw-again">再次输入新密码</label>
        <input id="pw-again" type="password" autoComplete="new-password" value={again} onChange={(event) => setAgain(event.target.value)} />
        {error && <ErrorBanner message={error} />}
        {done && (
          <div className="info-banner" role="status">
            {done}
          </div>
        )}
        <div className="button-row">
          <button type="submit" className="primary-button" disabled={busy || !current || !next}>
            {busy ? "保存中…" : "保存"}
          </button>
          <button type="button" className="text-button" onClick={onClose}>
            关闭
          </button>
        </div>
      </form>
    </Drawer>
  );
}

interface SessionRow {
  id: string;
  user_id: string;
  kind: string;
  label: string;
  created_at: number;
  expires_at: number;
  last_seen_at: number;
  user_agent: string;
  current: boolean;
}

function formatStamp(seconds: number | null | undefined): string {
  if (!seconds) return "—";
  return new Date(seconds * 1000).toLocaleString("zh-CN", { hour12: false });
}

export function UsersPage({ status, me }: { status: AuthStatus; me: AuthUser }) {
  const [users, setUsers] = useState<AuthUser[] | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState("");
  const [editing, setEditing] = useState<{ user: AuthUser | null } | null>(null);
  const [sessions, setSessions] = useState<SessionRow[] | null>(null);
  const [token, setToken] = useState<{ token: string; label: string } | null>(null);
  const enabled = status.enabled;
  async function load() {
    setError("");
    try {
      const listing = await request<{ items: AuthUser[] }>("/api/auth/users");
      setUsers(listing.items);
      if (enabled) setSessions((await request<{ items: SessionRow[] }>("/api/auth/sessions")).items);
    } catch (e) {
      setError(errorMessage(e));
      setUsers([]);
    }
  }
  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  async function act(id: string, body: Record<string, unknown> | null, path: string, done: string) {
    setBusy(id);
    setError("");
    setNotice("");
    try {
      await request(path, body ?? {});
      setNotice(done);
      await load();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy("");
    }
  }
  async function createToken() {
    const label = window.prompt("令牌用途（例如 Claude Code MCP）", "MCP 客户端");
    if (label === null) return;
    setBusy("token");
    setError("");
    try {
      const issued = await request<{ token: string; label: string }>("/api/auth/tokens", { label, days: 90 });
      setToken(issued);
      await load();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy("");
    }
  }
  return (
    <div className="page-content">
      <PageTitle
        title="用户与权限"
        description="账号、角色与页面可见性；只读角色不能修改任何数据，编辑者不能管理用户"
        action={
          <div className="button-row">
            <button type="button" className="secondary-button" disabled={!!busy} onClick={() => void load()}>
              <RefreshCw size={14} />
              刷新
            </button>
            {enabled && (
              <button type="button" className="secondary-button" disabled={!!busy} onClick={() => void createToken()}>
                <KeyRound size={14} />
                创建 API 令牌
              </button>
            )}
            <button type="button" className="primary-button" disabled={!!busy || !enabled} onClick={() => setEditing({ user: null })}>
              <Plus size={14} />
              新增用户
            </button>
          </div>
        }
      />
      {!enabled && (
        <div className="info-banner" role="status">
          登录未启用：当前所有请求都以本机管理员身份执行。启动服务前设置环境变量 <code>LATTICE_AUTH=1</code> 即可开启登录；首次打开页面时会引导创建管理员。
        </div>
      )}
      {error && <ErrorBanner message={error} />}
      {notice && (
        <div className="info-banner" role="status">
          {notice}
        </div>
      )}
      {token && (
        <div className="token-reveal" role="status">
          <b>令牌「{token.label}」已创建，只显示这一次，请立即复制：</b>
          <code>{token.token}</code>
          <small>
            MCP 客户端在请求头里携带 <code>Authorization: Bearer &lt;令牌&gt;</code>，例如：
            <code>claude mcp add --transport http lattice-metadata http://127.0.0.1:8787/api/metadata/mcp --header "Authorization: Bearer {token.token}"</code>
          </small>
          <button type="button" className="text-button" onClick={() => setToken(null)}>
            我已保存
          </button>
        </div>
      )}
      <section className="panel">
        <h2>
          <Users size={16} /> 用户
        </h2>
        {users === null ? (
          <Loading text="正在读取用户…" />
        ) : users.length ? (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>用户</th>
                  <th>角色</th>
                  <th>可见页面</th>
                  <th>状态</th>
                  <th>最近登录</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {users.map((user) => (
                  <tr key={user.id}>
                    <td>
                      <div className="quality-task-cell">
                        <span>
                          {user.display_name}
                          {user.id === me.id && <span className="neutral-badge">当前</span>}
                        </span>
                        <code>{user.username}</code>
                      </div>
                    </td>
                    <td>{user.role_label}</td>
                    <td>{user.page_override ? `${user.pages.length} 个页面` : "全部（按角色）"}</td>
                    <td>{user.disabled ? <span className="failure-badge">已停用</span> : <span className="healthy-badge">正常</span>}</td>
                    <td>{formatStamp(user.last_login_at)}</td>
                    <td>
                      <div className="quality-actions">
                        <button type="button" className="text-button" disabled={!!busy} onClick={() => setEditing({ user })}>
                          编辑
                        </button>
                        <button
                          type="button"
                          className="text-button"
                          disabled={!!busy || user.id === me.id}
                          onClick={() => void act(user.id, { disabled: !user.disabled }, `/api/auth/users/${user.id}/update`, user.disabled ? `已启用 ${user.username}。` : `已停用 ${user.username}，其登录会话已失效。`)}
                        >
                          {user.disabled ? "启用" : "停用"}
                        </button>
                        <button
                          type="button"
                          className="text-button"
                          disabled={!!busy || user.id === me.id}
                          onClick={() => {
                            if (window.confirm(`确认删除用户「${user.username}」？其会话与令牌会一并失效。`))
                              void act(user.id, {}, `/api/auth/users/${user.id}/delete`, `已删除 ${user.username}。`);
                          }}
                        >
                          <Trash2 size={13} />
                          删除
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty title={enabled ? "还没有用户" : "登录未启用"}>{enabled ? "点击“新增用户”创建账号。" : "启用登录后，第一个访问者会被引导创建管理员账号。"}</Empty>
        )}
      </section>
      <section className="panel">
        <h2>
          <ShieldCheck size={16} /> 角色
        </h2>
        <ul className="role-list">
          {status.roles.map((role) => (
            <li key={role.value}>
              <b>{role.label}</b>
              <code>{role.value}</code>
              <span>{role.description}</span>
            </li>
          ))}
        </ul>
      </section>
      {enabled && (
        <section className="panel">
          <h2>
            <UserCircle2 size={16} /> 我的会话与令牌
          </h2>
          {sessions === null ? (
            <Loading text="正在读取会话…" />
          ) : sessions.length ? (
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>类型</th>
                    <th>说明</th>
                    <th>创建</th>
                    <th>最近使用</th>
                    <th>到期</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {sessions.map((session) => (
                    <tr key={session.id}>
                      <td>
                        <span className="neutral-badge">{session.kind === "token" ? "API 令牌" : "登录会话"}</span>
                        {session.current && <span className="healthy-badge">当前</span>}
                      </td>
                      <td>{session.label || session.user_agent.slice(0, 60) || "—"}</td>
                      <td>{formatStamp(session.created_at)}</td>
                      <td>{formatStamp(session.last_seen_at)}</td>
                      <td>{formatStamp(session.expires_at)}</td>
                      <td>
                        <button
                          type="button"
                          className="text-button"
                          disabled={!!busy || session.current}
                          onClick={() => void act(session.id, {}, `/api/auth/sessions/${session.id}/revoke`, "已撤销。")}
                        >
                          撤销
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <Empty title="没有其他会话">当前只有这一个登录会话。</Empty>
          )}
        </section>
      )}
      {editing && (
        <UserEditor
          key={editing.user ? editing.user.id : "create"}
          user={editing.user}
          status={status}
          onClose={() => setEditing(null)}
          onSaved={(saved, mode) => {
            setEditing(null);
            setNotice(mode === "create" ? `已创建用户 ${saved.username}。` : `已更新用户 ${saved.username}。`);
            void load();
          }}
        />
      )}
    </div>
  );
}

function UserEditor({
  user,
  status,
  onClose,
  onSaved,
}: {
  user: AuthUser | null;
  status: AuthStatus;
  onClose: () => void;
  onSaved: (saved: AuthUser, mode: "create" | "edit") => void;
}) {
  const [username, setUsername] = useState(user?.username ?? "");
  const [displayName, setDisplayName] = useState(user?.display_name ?? "");
  const [role, setRole] = useState(user?.role ?? "viewer");
  const [password, setPassword] = useState("");
  const [limitPages, setLimitPages] = useState(!!user?.page_override);
  const [pages, setPages] = useState<string[]>(user?.pages ?? status.pages.map((page) => page.id));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function save(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const body: Record<string, unknown> = {
        display_name: displayName.trim() || null,
        role,
        pages: limitPages ? pages : [],
      };
      if (!user) body.username = username.trim();
      if (password) body.password = password;
      const saved = await request<AuthUser>(user ? `/api/auth/users/${user.id}/update` : "/api/auth/users", body);
      onSaved(saved, user ? "edit" : "create");
    } catch (e) {
      setError(errorMessage(e));
      setBusy(false);
    }
  }
  const selectable = status.pages.filter((page) => role === "admin" || page.id !== "users");
  return (
    <Drawer id="auth-user-editor" title={user ? `编辑用户 · ${user.username}` : "新增用户"} icon={<Users size={17} />} onClose={onClose} wide closeOnBackdrop={false}>
      <form className="quality-drawer-body" onSubmit={save}>
        <label htmlFor="user-username">用户名</label>
        <input id="user-username" value={username} disabled={!!user} autoComplete="off" onChange={(event) => setUsername(event.target.value)} />
        <label htmlFor="user-display">显示名称</label>
        <input id="user-display" value={displayName} onChange={(event) => setDisplayName(event.target.value)} />
        <label htmlFor="user-role">角色</label>
        <select id="user-role" value={role} onChange={(event) => setRole(event.target.value)}>
          {status.roles.map((item) => (
            <option key={item.value} value={item.value}>
              {item.label} · {item.description}
            </option>
          ))}
        </select>
        <label htmlFor="user-password">{user ? "重置密码（留空则不改）" : "初始密码"}</label>
        <input id="user-password" type="password" autoComplete="new-password" value={password} onChange={(event) => setPassword(event.target.value)} />
        <label className="checkbox-row">
          <input type="checkbox" checked={limitPages} onChange={(event) => setLimitPages(event.target.checked)} />
          只显示部分页面（页面可见性只影响导航，权限以角色为准）
        </label>
        {limitPages && (
          <div className="quality-check-list">
            {selectable.map((page) => (
              <label key={page.id}>
                <input
                  type="checkbox"
                  checked={pages.includes(page.id)}
                  onChange={(event) => setPages((current) => (event.target.checked ? [...current, page.id] : current.filter((item) => item !== page.id)))}
                />
                {page.label}
              </label>
            ))}
          </div>
        )}
        {error && <ErrorBanner message={error} />}
        <div className="button-row">
          <button type="submit" className="primary-button" disabled={busy || (!user && (!username.trim() || !password))}>
            {busy ? "保存中…" : "保存"}
          </button>
          <button type="button" className="text-button" disabled={busy} onClick={onClose}>
            取消
          </button>
        </div>
      </form>
    </Drawer>
  );
}
