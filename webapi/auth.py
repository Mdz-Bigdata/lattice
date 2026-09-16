# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements. See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership. The ASF licenses this file
# to you under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND.
# See the License for the specific language governing permissions
# and limitations under the License.

"""Accounts, sessions and roles of the platform (多用户与鉴权).

The platform started as a single-person local tool, so authentication is off
until ``LATTICE_AUTH=1``: with it off every request acts as a local
administrator and nothing here is consulted. With it on, the first visit
creates the administrator (``/api/auth/setup``), every other request needs a
session cookie or a bearer API token, and three roles decide what a request may
do:

* ``admin`` — everything, including user management;
* ``editor`` — everything except user management;
* ``viewer`` — read-only: GET requests plus the read-only query endpoints.

Pages are a separate, per-user list that only shapes the navigation; roles are
the boundary the API enforces. Accounts and sessions live in a SQLite file under
``.runtime`` so signing in never depends on PostgreSQL being reachable.
Passwords are stored as scrypt hashes; session and token secrets are stored
hashed, so the database never holds anything that logs a person in.
"""

from __future__ import annotations

import contextvars
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import env

SESSION_COOKIE = "lattice_session"
DEFAULT_SESSION_HOURS = 12
MAX_SESSION_HOURS = 24 * 30
DEFAULT_TOKEN_DAYS = 90
MAX_TOKEN_DAYS = 365
MIN_PASSWORD = 8
MAX_FAILURES = 5
FAILURE_WINDOW = 300
USERNAME = re.compile(r"^[A-Za-z0-9_.\-]{2,64}$")

ROLES: dict[str, dict[str, str]] = {
    "admin": {"label": "管理员", "description": "全部页面与接口，包括用户与权限管理。"},
    "editor": {"label": "编辑者", "description": "除用户管理外的全部功能，可修改规则、元数据与数据源。"},
    "viewer": {"label": "只读", "description": "只能浏览与执行只读查询，不能修改任何内容。"},
}
#: Navigation ids the WebUI knows, in menu order; a user's page list is a subset.
PAGES: tuple[tuple[str, str], ...] = (
    ("overview", "指标总览"),
    ("map", "数据地图"),
    ("sources", "数据源"),
    ("ingestion", "数据接入"),
    ("quality", "数据质量"),
    ("metadata", "元数据管理"),
    ("standards", "标准规范"),
    ("semantic", "语义模型"),
    ("metrics", "指标平台"),
    ("sql", "SQL 工作台"),
    ("services", "数据服务"),
    ("questions", "智能问数"),
    ("catalogs", "Catalog 管理"),
    ("identities", "身份与权限"),
    ("explorer", "API 控制台"),
    ("ops", "运行观测"),
    ("users", "用户与权限"),
)
PAGE_IDS = [page for page, _ in PAGES]
ADMIN_ONLY_PAGES = {"users"}
#: Requests that need no identity even when authentication is on.
OPEN_PATHS = {"/api/auth/status", "/api/auth/login", "/api/auth/setup", "/api/health"}
#: Write-shaped requests a viewer may still send: they only read data.
VIEWER_WRITES = (
    re.compile(r"^/api/query(/stream)?$"),
    re.compile(r"^/api/datasources/[^/]+/(query|preview)$"),
    re.compile(r"^/api/auth/(logout|password|tokens)$"),
    re.compile(r"^/api/metadata/(search|context/ask|context/tools/call)$"),
)
ADMIN_PATHS = (re.compile(r"^/api/auth/users(/|$)"), re.compile(r"^/api/auth/sessions(/|$)"))

#: The identity acting in the current request, read by services that record authors.
current_actor: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("lattice_actor", default=None)


class AuthError(ValueError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return digest.hex(), salt.hex()


def verify_password(password: str, stored_hash: str, salt_hex: str) -> bool:
    try:
        digest, _ = hash_password(password, bytes.fromhex(salt_hex))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, stored_hash)


def token_id(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AuthStore:
    """SQLite persistence for users and sessions; every statement is parameterised."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._memory: sqlite3.Connection | None = None
        if str(self.path) == ":memory:":
            self._memory = sqlite3.connect(":memory:", check_same_thread=False)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.bootstrap()

    def _connect(self) -> sqlite3.Connection:
        if self._memory is not None:
            return self._memory
        connection = sqlite3.connect(str(self.path), timeout=5, check_same_thread=False)
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _run(self, sql: str, params: tuple = (), *, fetch: str = "none") -> Any:
        with self._lock:
            connection = self._connect()
            try:
                connection.row_factory = sqlite3.Row
                cursor = connection.execute(sql, params)
                if fetch == "one":
                    row = cursor.fetchone()
                    result = dict(row) if row else None
                elif fetch == "all":
                    result = [dict(row) for row in cursor.fetchall()]
                else:
                    result = None
                connection.commit()
                return result
            finally:
                if self._memory is None:
                    connection.close()

    def bootstrap(self) -> None:
        self._run(
            """CREATE TABLE IF NOT EXISTS auth_user (
                 id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, display_name TEXT NOT NULL DEFAULT '',
                 password_hash TEXT NOT NULL, salt TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'viewer',
                 pages TEXT NOT NULL DEFAULT '', disabled INTEGER NOT NULL DEFAULT 0,
                 created_at REAL NOT NULL, updated_at REAL NOT NULL, last_login_at REAL)"""
        )
        self._run(
            """CREATE TABLE IF NOT EXISTS auth_session (
                 id TEXT PRIMARY KEY, user_id TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'session',
                 label TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, expires_at REAL NOT NULL,
                 last_seen_at REAL NOT NULL, user_agent TEXT NOT NULL DEFAULT '')"""
        )
        self._run("CREATE INDEX IF NOT EXISTS idx_auth_session_user ON auth_session (user_id)")

    # ----- users --------------------------------------------------------------------
    def count_users(self) -> int:
        row = self._run("SELECT count(1) AS n FROM auth_user", fetch="one")
        return int(row["n"]) if row else 0

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        return self._run("SELECT * FROM auth_user WHERE id = ?", (user_id,), fetch="one")

    def find_user(self, username: str) -> dict[str, Any] | None:
        return self._run("SELECT * FROM auth_user WHERE lower(username) = lower(?)", (username,), fetch="one")

    def list_users(self) -> list[dict[str, Any]]:
        return self._run("SELECT * FROM auth_user ORDER BY created_at, username", fetch="all")

    def insert_user(self, row: dict[str, Any]) -> None:
        self._run(
            """INSERT INTO auth_user (id, username, display_name, password_hash, salt, role, pages, disabled,
                                     created_at, updated_at, last_login_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
            (
                row["id"], row["username"], row["display_name"], row["password_hash"], row["salt"], row["role"],
                row["pages"], int(row["disabled"]), row["created_at"], row["updated_at"],
            ),
        )

    def update_user(self, user_id: str, fields: dict[str, Any]) -> None:
        allowed = {"display_name", "password_hash", "salt", "role", "pages", "disabled", "updated_at", "last_login_at"}
        keys = [key for key in fields if key in allowed]
        if not keys:
            return
        assignments = ", ".join(f"{key} = ?" for key in keys)
        self._run(f"UPDATE auth_user SET {assignments} WHERE id = ?", (*[fields[key] for key in keys], user_id))

    def delete_user(self, user_id: str) -> None:
        self._run("DELETE FROM auth_session WHERE user_id = ?", (user_id,))
        self._run("DELETE FROM auth_user WHERE id = ?", (user_id,))

    # ----- sessions and tokens ----------------------------------------------------
    def insert_session(self, row: dict[str, Any]) -> None:
        self._run(
            """INSERT INTO auth_session (id, user_id, kind, label, created_at, expires_at, last_seen_at, user_agent)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (row["id"], row["user_id"], row["kind"], row["label"], row["created_at"], row["expires_at"], row["last_seen_at"], row["user_agent"]),
        )

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        return self._run("SELECT * FROM auth_session WHERE id = ?", (session_id,), fetch="one")

    def touch_session(self, session_id: str, seen: float, expires: float) -> None:
        self._run("UPDATE auth_session SET last_seen_at = ?, expires_at = ? WHERE id = ?", (seen, expires, session_id))

    def delete_session(self, session_id: str) -> bool:
        before = self.get_session(session_id)
        self._run("DELETE FROM auth_session WHERE id = ?", (session_id,))
        return before is not None

    def sessions_for(self, user_id: str | None = None, kind: str | None = None) -> list[dict[str, Any]]:
        conditions, params = [], []
        if user_id:
            conditions.append("user_id = ?")
            params.append(user_id)
        if kind:
            conditions.append("kind = ?")
            params.append(kind)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        return self._run(f"SELECT * FROM auth_session{where} ORDER BY created_at DESC", tuple(params), fetch="all")

    def delete_sessions_for(self, user_id: str, *, kind: str | None = None, keep: str | None = None) -> int:
        rows = self.sessions_for(user_id, kind)
        removed = 0
        for row in rows:
            if keep and row["id"] == keep:
                continue
            self._run("DELETE FROM auth_session WHERE id = ?", (row["id"],))
            removed += 1
        return removed

    def sweep(self, now: float) -> int:
        rows = self._run("SELECT id FROM auth_session WHERE expires_at <= ?", (now,), fetch="all")
        for row in rows:
            self._run("DELETE FROM auth_session WHERE id = ?", (row["id"],))
        return len(rows)


class AuthService:
    """Sign-in, sessions, tokens, users and the role checks the middleware applies."""

    def __init__(
        self,
        store: AuthStore,
        *,
        enabled: bool,
        session_hours: int = DEFAULT_SESSION_HOURS,
        clock: Callable[[], float] = time.time,
    ):
        self.store = store
        self.enabled = bool(enabled)
        self.session_hours = max(1, min(int(session_hours), MAX_SESSION_HOURS))
        self._clock = clock
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls, runtime: Path) -> "AuthService":
        enabled = env("AUTH", "0").strip().lower() in {"1", "true", "on", "yes"}
        raw_path = env("AUTH_DB", "").strip()
        path = Path(raw_path) if raw_path else runtime / "auth.sqlite"
        try:
            hours = int(env("SESSION_HOURS", str(DEFAULT_SESSION_HOURS)))
        except ValueError:
            hours = DEFAULT_SESSION_HOURS
        return cls(AuthStore(path), enabled=enabled, session_hours=hours)

    # ----- presentation -------------------------------------------------------------
    @staticmethod
    def public(user: dict[str, Any] | None) -> dict[str, Any] | None:
        if user is None:
            return None
        pages = AuthService.allowed_pages(user)
        return {
            "id": user["id"],
            "username": user["username"],
            "display_name": user.get("display_name") or user["username"],
            "role": user["role"],
            "role_label": ROLES.get(user["role"], {}).get("label", user["role"]),
            "pages": pages,
            "page_override": bool(user.get("pages")),
            "disabled": bool(user.get("disabled")),
            "created_at": user.get("created_at"),
            "last_login_at": user.get("last_login_at"),
        }

    @staticmethod
    def allowed_pages(user: dict[str, Any]) -> list[str]:
        role = user.get("role") or "viewer"
        base = [page for page in PAGE_IDS if role == "admin" or page not in ADMIN_ONLY_PAGES]
        override = [page for page in str(user.get("pages") or "").split(",") if page]
        return [page for page in base if not override or page in override]

    def local_admin(self) -> dict[str, Any]:
        """Who acts when authentication is off: the person at the keyboard."""
        return {"id": "local", "username": "local", "display_name": "本机用户", "role": "admin", "pages": "", "disabled": 0}

    def status(self, user: dict[str, Any] | None) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "setup_required": self.enabled and self.store.count_users() == 0,
            "user": self.public(user if self.enabled else self.local_admin()),
            "roles": [{"value": key, **value} for key, value in ROLES.items()],
            "pages": [{"id": page, "label": label} for page, label in PAGES],
            "session_hours": self.session_hours,
        }

    # ----- accounts -------------------------------------------------------------------
    def _check_username(self, username: Any) -> str:
        name = str(username or "").strip()
        if not USERNAME.match(name):
            raise AuthError(400, "用户名只能包含字母、数字、点、下划线或连字符，长度 2 到 64。")
        return name

    @staticmethod
    def _check_password(password: Any) -> str:
        text = str(password or "")
        if len(text) < MIN_PASSWORD or len(text) > 128:
            raise AuthError(400, f"密码长度须在 {MIN_PASSWORD} 到 128 个字符之间。")
        return text

    @staticmethod
    def _check_role(role: Any) -> str:
        value = str(role or "viewer").strip()
        if value not in ROLES:
            raise AuthError(400, "角色只能是 admin、editor 或 viewer。")
        return value

    @staticmethod
    def _check_pages(pages: Any) -> str:
        if pages in (None, "", []):
            return ""
        if not isinstance(pages, list):
            raise AuthError(400, "页面列表必须是数组。")
        unknown = [str(page) for page in pages if str(page) not in PAGE_IDS]
        if unknown:
            raise AuthError(400, "未知的页面：" + "、".join(unknown[:5]))
        return ",".join(page for page in PAGE_IDS if page in {str(item) for item in pages})

    def setup(self, username: Any, password: Any, display_name: Any = "") -> dict[str, Any]:
        """Create the first administrator; refused once any account exists."""
        if not self.enabled:
            raise AuthError(400, "未启用登录（LATTICE_AUTH=0），无需初始化。")
        if self.store.count_users() > 0:
            raise AuthError(409, "管理员已经创建，请直接登录。")
        return self.public(self._create(username, password, display_name, "admin", ""))

    def _create(self, username: Any, password: Any, display_name: Any, role: str, pages: str) -> dict[str, Any]:
        name = self._check_username(username)
        if self.store.find_user(name) is not None:
            raise AuthError(409, f"用户名 {name} 已存在。")
        digest, salt = hash_password(self._check_password(password))
        now = self._clock()
        row = {
            "id": secrets.token_hex(8),
            "username": name,
            "display_name": str(display_name or "").strip()[:100] or name,
            "password_hash": digest,
            "salt": salt,
            "role": role,
            "pages": pages,
            "disabled": 0,
            "created_at": now,
            "updated_at": now,
        }
        self.store.insert_user(row)
        return self.store.get_user(row["id"]) or row

    def create_user(self, actor: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
        self._require_admin(actor)
        role = self._check_role(data.get("role"))
        pages = self._check_pages(data.get("pages"))
        return self.public(self._create(data.get("username"), data.get("password"), data.get("display_name"), role, pages))

    def list_users(self, actor: dict[str, Any]) -> list[dict[str, Any]]:
        self._require_admin(actor)
        return [self.public(row) for row in self.store.list_users()]

    def update_user(self, actor: dict[str, Any], user_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        self._require_admin(actor)
        user = self.store.get_user(str(user_id))
        if user is None:
            raise AuthError(404, "用户不存在。")
        fields: dict[str, Any] = {"updated_at": self._clock()}
        if patch.get("display_name") is not None:
            fields["display_name"] = str(patch["display_name"]).strip()[:100] or user["username"]
        if patch.get("role") is not None:
            fields["role"] = self._check_role(patch["role"])
        if "pages" in patch:
            fields["pages"] = self._check_pages(patch["pages"])
        if patch.get("disabled") is not None:
            fields["disabled"] = 1 if patch["disabled"] else 0
        if patch.get("password"):
            fields["password_hash"], fields["salt"] = hash_password(self._check_password(patch["password"]))
        demoted = fields.get("role", user["role"]) != "admin" or fields.get("disabled", user["disabled"])
        if user["role"] == "admin" and demoted and self._enabled_admins(exclude=user["id"]) == 0:
            raise AuthError(400, "至少要保留一个可用的管理员账号。")
        self.store.update_user(user["id"], fields)
        if fields.get("disabled") or "password_hash" in fields or "role" in fields:
            self.store.delete_sessions_for(user["id"], kind="session")
        return self.public(self.store.get_user(user["id"]))

    def delete_user(self, actor: dict[str, Any], user_id: str) -> dict[str, Any]:
        self._require_admin(actor)
        user = self.store.get_user(str(user_id))
        if user is None:
            raise AuthError(404, "用户不存在。")
        if user["id"] == actor.get("id"):
            raise AuthError(400, "不能删除当前登录的账号。")
        if user["role"] == "admin" and self._enabled_admins(exclude=user["id"]) == 0:
            raise AuthError(400, "至少要保留一个可用的管理员账号。")
        self.store.delete_user(user["id"])
        return {"id": user["id"], "deleted": True}

    def _enabled_admins(self, *, exclude: str | None = None) -> int:
        return sum(1 for row in self.store.list_users() if row["role"] == "admin" and not row["disabled"] and row["id"] != exclude)

    @staticmethod
    def _require_admin(actor: dict[str, Any] | None) -> None:
        if not actor or actor.get("role") != "admin":
            raise AuthError(403, "只有管理员可以管理用户。")

    def change_password(self, user: dict[str, Any], current: Any, new: Any, *, keep_session: str | None = None) -> dict[str, Any]:
        stored = self.store.get_user(user["id"])
        if stored is None:
            raise AuthError(404, "用户不存在。")
        if not verify_password(str(current or ""), stored["password_hash"], stored["salt"]):
            raise AuthError(400, "当前密码不正确。")
        digest, salt = hash_password(self._check_password(new))
        self.store.update_user(stored["id"], {"password_hash": digest, "salt": salt, "updated_at": self._clock()})
        revoked = self.store.delete_sessions_for(stored["id"], kind="session", keep=keep_session)
        return {"changed": True, "revoked_sessions": revoked}

    # ----- sign-in and sessions ---------------------------------------------------------
    def login(self, username: Any, password: Any, user_agent: str = "") -> tuple[dict[str, Any], str]:
        if not self.enabled:
            raise AuthError(400, "未启用登录（LATTICE_AUTH=0）。")
        name = str(username or "").strip()
        self._check_failures(name)
        user = self.store.find_user(name) if name else None
        if user is None or not verify_password(str(password or ""), user["password_hash"], user["salt"]):
            self._record_failure(name)
            raise AuthError(401, "用户名或密码不正确。")
        if user["disabled"]:
            raise AuthError(403, "该账号已停用，请联系管理员。")
        with self._lock:
            self._failures.pop(name.lower(), None)
        token = self._open(user, "session", "", self.session_hours * 3600, user_agent)
        self.store.update_user(user["id"], {"last_login_at": self._clock()})
        return self.store.get_user(user["id"]) or user, token

    def _check_failures(self, name: str) -> None:
        now = self._clock()
        with self._lock:
            recent = [moment for moment in self._failures.get(name.lower(), []) if now - moment < FAILURE_WINDOW]
            self._failures[name.lower()] = recent
            if len(recent) >= MAX_FAILURES:
                raise AuthError(429, "登录失败次数过多，请 5 分钟后再试。")

    def _record_failure(self, name: str) -> None:
        with self._lock:
            self._failures.setdefault(name.lower(), []).append(self._clock())

    def _open(self, user: dict[str, Any], kind: str, label: str, ttl: float, user_agent: str) -> str:
        token = secrets.token_urlsafe(32)
        now = self._clock()
        self.store.insert_session(
            {
                "id": token_id(token),
                "user_id": user["id"],
                "kind": kind,
                "label": label[:100],
                "created_at": now,
                "expires_at": now + ttl,
                "last_seen_at": now,
                "user_agent": str(user_agent or "")[:200],
            }
        )
        return token

    def resolve(self, token: str | None) -> dict[str, Any] | None:
        """The user behind a session cookie or API token; sessions slide forward when used."""
        if not token:
            return None
        session = self.store.get_session(token_id(token))
        now = self._clock()
        if session is None or session["expires_at"] <= now:
            if session is not None:
                self.store.delete_session(session["id"])
            return None
        user = self.store.get_user(session["user_id"])
        if user is None or user["disabled"]:
            return None
        if session["kind"] == "session":
            ttl = self.session_hours * 3600
            if session["expires_at"] - now < ttl * 0.9:
                self.store.touch_session(session["id"], now, now + ttl)
        else:
            self.store.touch_session(session["id"], now, session["expires_at"])
        return {**user, "session_id": session["id"], "session_kind": session["kind"]}

    def logout(self, token: str | None) -> bool:
        return bool(token) and self.store.delete_session(token_id(token))

    def create_token(self, user: dict[str, Any], label: Any = "", days: Any = DEFAULT_TOKEN_DAYS) -> dict[str, Any]:
        """A long-lived bearer token for MCP clients and scripts; shown once."""
        if user.get("role") == "viewer":
            raise AuthError(403, "只读账号不能创建访问令牌。")
        try:
            span = int(days or DEFAULT_TOKEN_DAYS)
        except (TypeError, ValueError) as error:
            raise AuthError(400, "有效天数必须是整数。") from error
        if span < 1 or span > MAX_TOKEN_DAYS:
            raise AuthError(400, f"有效天数须在 1 到 {MAX_TOKEN_DAYS} 之间。")
        token = self._open(user, "token", str(label or "").strip() or "API 令牌", span * 86400, "api-token")
        return {"token": token, "id": token_id(token), "label": str(label or "").strip() or "API 令牌", "expires_at": self._clock() + span * 86400}

    def sessions(self, actor: dict[str, Any], user_id: str | None = None) -> list[dict[str, Any]]:
        target = user_id or actor["id"]
        if target != actor["id"]:
            self._require_admin(actor)
        return [self._session_view(row, actor) for row in self.store.sessions_for(target)]

    def _session_view(self, row: dict[str, Any], actor: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "user_id": row["user_id"],
            "kind": row["kind"],
            "label": row["label"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "last_seen_at": row["last_seen_at"],
            "user_agent": row["user_agent"],
            "current": row["id"] == actor.get("session_id"),
        }

    def revoke(self, actor: dict[str, Any], session_id: str) -> dict[str, Any]:
        row = self.store.get_session(str(session_id))
        if row is None:
            raise AuthError(404, "会话或令牌不存在。")
        if row["user_id"] != actor["id"]:
            self._require_admin(actor)
        self.store.delete_session(row["id"])
        return {"id": row["id"], "revoked": True}

    def sweep(self) -> dict[str, Any]:
        return {"removed": self.store.sweep(self._clock())}

    # ----- the request gate --------------------------------------------------------------
    def gate(self, path: str, method: str, cookie_token: str | None, bearer: str | None) -> tuple[dict[str, Any] | None, tuple[int, str] | None]:
        """Identify the caller and decide; returns (user, refusal)."""
        if not self.enabled:
            return self.local_admin(), None
        user = self.resolve(bearer) if bearer else self.resolve(cookie_token)
        if path in OPEN_PATHS:
            # Open routes still learn who is calling, so /status can report the signed-in user.
            return user, None
        if user is None:
            return None, (401, "请先登录。")
        refusal = self.authorize(user, path, method)
        return user, refusal

    @staticmethod
    def authorize(user: dict[str, Any], path: str, method: str) -> tuple[int, str] | None:
        role = user.get("role") or "viewer"
        if any(pattern.match(path) for pattern in ADMIN_PATHS) and role != "admin":
            if not (path == "/api/auth/sessions" and method == "GET"):
                return 403, "只有管理员可以管理用户。"
        if role == "viewer" and method not in {"GET", "HEAD", "OPTIONS"}:
            if not any(pattern.match(path) for pattern in VIEWER_WRITES):
                return 403, "只读账号不能修改数据。"
        if path == "/api/metadata/mcp" and role == "viewer":
            return 403, "只读账号不能使用 MCP 服务。"
        return None


def parse_bearer(header: str | None) -> str | None:
    if not header:
        return None
    parts = header.strip().split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1].strip():
        return parts[1].strip()
    return None


def actor_name(default: str = "admin") -> str:
    """The username acting in the current request, for services that record authors."""
    actor = current_actor.get()
    return str(actor.get("username") or default) if actor else default


def dump_pages(pages: Any) -> str:
    return json.dumps(pages, ensure_ascii=False)
