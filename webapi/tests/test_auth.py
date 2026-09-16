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

"""Accounts, sessions, roles and the request gate of 多用户与鉴权."""

import json

import pytest
from fastapi.testclient import TestClient

from webapi import app as app_module
from webapi.auth import SESSION_COOKIE, AuthError, AuthService, AuthStore, hash_password, parse_bearer, verify_password


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def service():
    return AuthService(AuthStore(":memory:"), enabled=True, session_hours=2, clock=Clock())


# ----- service -------------------------------------------------------------------------
def test_passwords_are_hashed_with_a_salt_and_verified_in_constant_time():
    digest, salt = hash_password("correct horse")
    assert verify_password("correct horse", digest, salt) and not verify_password("wrong", digest, salt)
    assert hash_password("correct horse")[0] != digest  # a fresh salt every time
    assert parse_bearer("Bearer abc") == "abc" and parse_bearer("Basic x") is None and parse_bearer(None) is None


def test_setup_creates_the_only_admin_and_login_opens_a_sliding_session(service):
    status = service.status(None)
    assert status["enabled"] and status["setup_required"] and status["user"] is None
    admin = service.setup("root", "secret-pass", "管理员")
    assert admin["role"] == "admin" and admin["pages"][-1] == "users"
    with pytest.raises(AuthError, match="已经创建"):
        service.setup("again", "secret-pass")
    with pytest.raises(AuthError, match="不正确"):
        service.login("root", "nope")
    user, token = service.login("root", "secret-pass", "pytest")
    assert user["last_login_at"] == 1_000_000.0
    resolved = service.resolve(token)
    assert resolved["username"] == "root" and resolved["session_kind"] == "session"
    service._clock.now += 3600  # halfway through: the session slides forward
    assert service.resolve(token) is not None
    service._clock.now += 7000
    assert service.resolve(token) is not None  # extended at the previous use
    service._clock.now += 8000
    assert service.resolve(token) is None
    assert service.logout("nope") is False


def test_login_failures_are_rate_limited_per_account(service):
    service.setup("root", "secret-pass")
    for _ in range(5):
        with pytest.raises(AuthError):
            service.login("root", "wrong")
    with pytest.raises(AuthError, match="5 分钟"):
        service.login("root", "secret-pass")
    service._clock.now += 301
    assert service.login("root", "secret-pass")[0]["username"] == "root"


def test_user_management_keeps_one_admin_and_revokes_sessions(service):
    service.setup("root", "secret-pass")
    root = service.store.find_user("root")
    viewer = service.create_user(root, {"username": "vera", "password": "viewer-pass", "role": "viewer", "pages": ["sql", "questions"]})
    assert viewer["pages"] == ["sql", "questions"] and viewer["page_override"] is True
    with pytest.raises(AuthError, match="页面"):
        service.create_user(root, {"username": "x", "password": "viewer-pass", "pages": ["nope"]})
    with pytest.raises(AuthError, match="已存在"):
        service.create_user(root, {"username": "VERA", "password": "viewer-pass"})
    with pytest.raises(AuthError, match="只有管理员"):
        service.create_user(service.store.find_user("vera"), {"username": "y", "password": "viewer-pass"})
    _, token = service.login("vera", "viewer-pass")
    service.update_user(root, viewer["id"], {"role": "editor"})
    assert service.resolve(token) is None  # a role change ends existing sessions
    with pytest.raises(AuthError, match="保留一个可用的管理员"):
        service.update_user(root, root["id"], {"role": "viewer"})
    with pytest.raises(AuthError, match="当前登录"):
        service.delete_user(root, root["id"])
    assert service.delete_user(root, viewer["id"])["deleted"] is True
    assert [item["username"] for item in service.list_users(root)] == ["root"]


def test_password_change_and_tokens(service):
    service.setup("root", "secret-pass")
    root, token = service.login("root", "secret-pass")
    _, other = service.login("root", "secret-pass")
    current = service.resolve(token)
    with pytest.raises(AuthError, match="当前密码"):
        service.change_password(current, "wrong", "another-pass")
    outcome = service.change_password(current, "secret-pass", "another-pass", keep_session=current["session_id"])
    assert outcome["revoked_sessions"] == 1 and service.resolve(other) is None and service.resolve(token) is not None
    issued = service.create_token(current, "MCP", 30)
    assert service.resolve(issued["token"])["session_kind"] == "token"
    service._clock.now += 31 * 86400
    assert service.resolve(issued["token"]) is None
    viewer = service.create_user(current, {"username": "vera", "password": "viewer-pass", "role": "viewer"})
    with pytest.raises(AuthError, match="只读账号"):
        service.create_token(service.store.get_user(viewer["id"]))


def test_gate_and_role_rules(service):
    assert AuthService(AuthStore(":memory:"), enabled=False).gate("/api/quality/rules", "POST", None, None)[0]["role"] == "admin"
    assert service.gate("/api/auth/status", "GET", None, None) == (None, None)
    assert service.gate("/api/quality/rules", "GET", None, None) == (None, (401, "请先登录。"))
    service.setup("root", "secret-pass")
    root = service.store.find_user("root")
    viewer = service.store.get_user(service.create_user(root, {"username": "vera", "password": "viewer-pass", "role": "viewer"})["id"])
    editor = service.store.get_user(service.create_user(root, {"username": "eve", "password": "editor-pass", "role": "editor"})["id"])
    assert service.authorize(viewer, "/api/quality/rules", "POST") == (403, "只读账号不能修改数据。")
    assert service.authorize(viewer, "/api/query", "POST") is None
    assert service.authorize(viewer, "/api/datasources/x/query", "POST") is None
    assert service.authorize(viewer, "/api/metadata/mcp", "POST")[0] == 403
    assert service.authorize(editor, "/api/quality/rules", "POST") is None
    assert service.authorize(editor, "/api/auth/users", "GET") == (403, "只有管理员可以管理用户。")
    assert service.authorize(editor, "/api/auth/sessions", "GET") is None
    assert service.authorize(root, "/api/auth/users", "POST") is None


# ----- routes --------------------------------------------------------------------------
@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LATTICE_AUTH", "1")
    monkeypatch.setenv("LATTICE_AUTH_DB", str(tmp_path / "auth.sqlite"))
    monkeypatch.setattr(app_module, "RUNTIME", tmp_path / "web")
    monkeypatch.setattr(app_module, "POLARIS_RUNTIME", tmp_path / "polaris")
    monkeypatch.setattr(app_module, "ENGINES_RUNTIME", tmp_path / "engines")
    with TestClient(app_module.app, base_url="http://127.0.0.1:8787") as value:
        yield value


def test_api_requires_setup_then_login_and_enforces_roles(client):
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/datasources").status_code == 401
    status = client.get("/api/auth/status").json()
    assert status["enabled"] and status["setup_required"] and status["user"] is None
    created = client.post("/api/auth/setup", json={"username": "root", "password": "secret-pass", "display_name": "管理员"})
    assert created.status_code == 200 and SESSION_COOKIE in created.cookies
    assert client.get("/api/auth/status").json()["user"]["username"] == "root"
    assert client.get("/api/datasources").status_code == 200
    assert client.post("/api/auth/setup", json={"username": "xx", "password": "secret-pass"}).status_code == 409
    viewer = client.post("/api/auth/users", json={"username": "vera", "password": "viewer-pass", "role": "viewer", "pages": ["sql"]}).json()
    assert viewer["role_label"] == "只读" and viewer["pages"] == ["sql"]
    glossary = client.post("/api/metadata/entities", json={"entity_type": "glossary", "name": "auth-glossary"})
    assert glossary.status_code == 200
    feed = client.get("/api/metadata/feed", params={"entity_fqn": "auth-glossary"}).json()
    assert "root" in json.dumps(feed["items"][0], ensure_ascii=False)
    assert client.post("/api/auth/logout", json={}).json()["logged_out"] is True
    client.cookies.clear()
    assert client.get("/api/datasources").status_code == 401

    # The same client signs in as the viewer: one app, one lifespan, a fresh cookie jar.
    assert client.post("/api/auth/login", json={"username": "vera", "password": "wrong"}).status_code == 401
    assert client.post("/api/auth/login", json={"username": "vera", "password": "viewer-pass"}).status_code == 200
    assert client.get("/api/quality/rules").status_code == 200
    refused = client.post("/api/quality/rules", json={"name": "x", "metric": "column_null", "datasource_id": "local-sample", "table_name": "t", "column_name": "c"})
    assert refused.status_code == 403 and "只读" in refused.json()["detail"]
    assert client.post("/api/query", json={"sql": "SELECT 1 AS one", "datasource_id": "local-sample"}).status_code == 200
    assert client.get("/api/auth/users").status_code == 403
    assert client.post("/api/auth/tokens", json={"label": "x"}).status_code == 403
    assert client.get("/api/auth/me").json()["username"] == "vera"


def test_api_tokens_authenticate_mcp_clients_and_disabled_users_are_locked_out(client):
    client.post("/api/auth/setup", json={"username": "root", "password": "secret-pass"})
    editor = client.post("/api/auth/users", json={"username": "eve", "password": "editor-pass", "role": "editor"}).json()
    root_cookie = dict(client.cookies)
    client.cookies.clear()
    client.post("/api/auth/login", json={"username": "eve", "password": "editor-pass"})
    issued = client.post("/api/auth/tokens", json={"label": "Claude Code", "days": 7}).json()
    assert issued["token"] and issued["label"] == "Claude Code"
    assert client.get("/api/auth/sessions").json()["items"][0]["kind"] == "token"
    assert client.get("/api/auth/users").status_code == 403
    client.cookies.clear()
    bearer = {"Authorization": f"Bearer {issued['token']}"}
    assert client.get("/api/metadata/health", headers=bearer).status_code == 200
    initialised = client.post(
        "/api/metadata/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}},
        headers={**bearer, "Accept": "application/json, text/event-stream"},
    )
    assert initialised.status_code == 200
    assert client.get("/api/metadata/health", headers={"Authorization": "Bearer nope"}).status_code == 401
    for name, value in root_cookie.items():
        client.cookies.set(name, value)
    assert client.post(f"/api/auth/users/{editor['id']}/update", json={"disabled": True}).json()["disabled"] is True
    assert client.get("/api/metadata/health", headers=bearer).status_code == 401
    client.cookies.clear()
    assert client.post("/api/auth/login", json={"username": "eve", "password": "editor-pass"}).status_code == 403
    for name, value in root_cookie.items():
        client.cookies.set(name, value)
    assert client.post(f"/api/auth/users/{editor['id']}/delete", json={}).json()["deleted"] is True
    assert client.post("/api/auth/password", json={"current_password": "secret-pass", "new_password": "another-pass"}).json()["changed"] is True
    assert client.get("/api/auth/me").status_code == 200


def test_auth_off_acts_as_the_local_administrator(tmp_path, monkeypatch):
    monkeypatch.setenv("LATTICE_AUTH", "0")
    monkeypatch.setattr(app_module, "RUNTIME", tmp_path / "web")
    monkeypatch.setattr(app_module, "POLARIS_RUNTIME", tmp_path / "polaris")
    monkeypatch.setattr(app_module, "ENGINES_RUNTIME", tmp_path / "engines")
    with TestClient(app_module.app, base_url="http://127.0.0.1:8787") as local:
        status = local.get("/api/auth/status").json()
        assert status["enabled"] is False and status["user"]["role"] == "admin" and status["user"]["id"] == "local"
        assert local.get("/api/auth/users").json()["items"] == []
        assert local.post("/api/auth/login", json={"username": "a", "password": "b"}).status_code == 400
        assert local.post("/api/auth/tokens", json={}).status_code == 400
