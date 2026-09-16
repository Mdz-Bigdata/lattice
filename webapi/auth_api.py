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

"""HTTP routes of 多用户与鉴权: sign-in, the current user, users, sessions and tokens.

The middleware in ``webapi/app.py`` has already identified the caller (or let
the request through because authentication is off) by the time a route here
runs, and left the user on ``request.state.user``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from .auth import SESSION_COOKIE, AuthError, AuthService

router = APIRouter(prefix="/api/auth")


class EmptyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SetupInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=1, max_length=128)
    display_name: str | None = Field(default=None, max_length=100)


class LoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class PasswordInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=1, max_length=128)


class CreateUserInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=1, max_length=128)
    display_name: str | None = Field(default=None, max_length=100)
    role: str = Field(default="viewer", max_length=16)
    pages: list[str] | None = Field(default=None, max_length=50)


class UpdateUserInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str | None = Field(default=None, max_length=100)
    role: str | None = Field(default=None, max_length=16)
    pages: list[str] | None = Field(default=None, max_length=50)
    disabled: bool | None = None
    password: str | None = Field(default=None, max_length=128)


class TokenInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str | None = Field(default=None, max_length=100)
    days: int | None = Field(default=None, ge=1, le=365)


def guarded(operation, *args, **kwargs):
    try:
        return operation(*args, **kwargs)
    except HTTPException:
        raise
    except (AuthError, ValueError, KeyError, OSError) as error:
        status = getattr(error, "status_code", None)
        raise HTTPException(status if isinstance(status, int) else 400, str(error)[:400]) from error


def _auth(request: Request) -> AuthService:
    service = getattr(request.app.state, "auth", None)
    if service is None:
        raise HTTPException(503, "登录模块尚未初始化。")
    return service


def _user(request: Request) -> dict[str, Any]:
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(401, "请先登录。")
    return user


def _set_cookie(request: Request, response: Response, token: str, hours: int) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=hours * 3600,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )


@router.get("/status")
def auth_status(request: Request):
    service = _auth(request)
    return service.status(getattr(request.state, "user", None))


@router.post("/setup")
def auth_setup(payload: SetupInput, request: Request, response: Response):
    service = _auth(request)
    user = guarded(service.setup, payload.username, payload.password, payload.display_name)
    _, token = guarded(service.login, payload.username, payload.password, request.headers.get("user-agent", ""))
    _set_cookie(request, response, token, service.session_hours)
    return {"user": user, "logged_in": True}


@router.post("/login")
def auth_login(payload: LoginInput, request: Request, response: Response):
    service = _auth(request)
    user, token = guarded(service.login, payload.username, payload.password, request.headers.get("user-agent", ""))
    _set_cookie(request, response, token, service.session_hours)
    return {"user": service.public(user), "logged_in": True}


@router.post("/logout")
def auth_logout(payload: EmptyInput, request: Request, response: Response):
    service = _auth(request)
    service.logout(request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"logged_out": True}


@router.get("/me")
def auth_me(request: Request):
    service = _auth(request)
    return service.public(_user(request))


@router.post("/password")
def auth_password(payload: PasswordInput, request: Request):
    service = _auth(request)
    user = _user(request)
    if user.get("id") == "local":
        raise HTTPException(400, "未启用登录，没有可修改的密码。")
    return guarded(service.change_password, user, payload.current_password, payload.new_password, keep_session=user.get("session_id"))


@router.get("/users")
def list_users(request: Request):
    return {"items": guarded(_auth(request).list_users, _user(request))}


@router.post("/users")
def create_user(payload: CreateUserInput, request: Request):
    return guarded(_auth(request).create_user, _user(request), payload.model_dump())


@router.post("/users/{user_id}/update")
def update_user(user_id: str, payload: UpdateUserInput, request: Request):
    return guarded(_auth(request).update_user, _user(request), user_id, payload.model_dump(exclude_unset=True))


@router.post("/users/{user_id}/delete")
def delete_user(user_id: str, payload: EmptyInput, request: Request):
    return guarded(_auth(request).delete_user, _user(request), user_id)


@router.get("/sessions")
def list_sessions(request: Request, user_id: str | None = None):
    return {"items": guarded(_auth(request).sessions, _user(request), user_id)}


@router.post("/sessions/{session_id}/revoke")
def revoke_session(session_id: str, payload: EmptyInput, request: Request):
    return guarded(_auth(request).revoke, _user(request), session_id)


@router.post("/tokens")
def create_token(payload: TokenInput, request: Request):
    user = _user(request)
    if user.get("id") == "local":
        raise HTTPException(400, "未启用登录时无需访问令牌。")
    return guarded(_auth(request).create_token, user, payload.label, payload.days)
