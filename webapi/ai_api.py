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

"""HTTP routes of AI 增强: rule suggestions, semantic model drafts, root-cause analysis."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .ai_assist import AiAssistError, AiAssistService
from .connectors import ConnectorError
from .datasources import DataSourceError
from .llm import LlmError
from .metadata_service import MetadataError
from .metadata_store import MetadataStoreError
from .quality_metrics import MetricError
from .quality_service import QualityError
from .quality_store import QualityStoreError

router = APIRouter(prefix="/api/ai")


class SuggestRulesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    datasource_id: str = Field(min_length=1, max_length=64)
    schema_name: str | None = Field(default=None, max_length=200)
    table_name: str = Field(min_length=1, max_length=200)
    use_model: bool = True


class ApplyRulesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rules: list[dict[str, Any]] = Field(min_length=1, max_length=50)


class SuggestModelInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    datasource_id: str = Field(min_length=1, max_length=64)
    schema_name: str | None = Field(default=None, max_length=200)
    tables: list[str] = Field(min_length=1, max_length=30)
    name: str | None = Field(default=None, max_length=120)
    use_model: bool = True


class RootCauseInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_type: str | None = Field(default=None, max_length=64)
    ref: str = Field(min_length=1, max_length=1024)
    days: int = Field(default=7, ge=1, le=90)
    use_model: bool = True


def guarded(operation, *args, **kwargs):
    try:
        return operation(*args, **kwargs)
    except HTTPException:
        raise
    except (
        AiAssistError, QualityError, QualityStoreError, MetricError, DataSourceError, ConnectorError,
        MetadataError, MetadataStoreError, LlmError, ValueError, KeyError, OSError,
    ) as error:
        status = getattr(error, "status_code", None)
        raise HTTPException(status if isinstance(status, int) else 400, str(error)[:800]) from error


def _ai(request: Request) -> AiAssistService:
    service = getattr(request.app.state, "ai", None)
    if service is None:
        raise HTTPException(503, "AI 增强模块尚未初始化。")
    return service


@router.get("/status")
def ai_status(request: Request):
    return _ai(request).status()


@router.post("/quality-rules/suggest")
def suggest_rules(payload: SuggestRulesInput, request: Request):
    return guarded(_ai(request).suggest_rules, payload.datasource_id, payload.schema_name, payload.table_name, use_model=payload.use_model)


@router.post("/quality-rules/apply")
def apply_rules(payload: ApplyRulesInput, request: Request):
    return guarded(_ai(request).apply_rules, payload.rules)


@router.post("/semantic-model/suggest")
def suggest_model(payload: SuggestModelInput, request: Request):
    return guarded(_ai(request).suggest_semantic_model, payload.datasource_id, payload.schema_name, payload.tables, name=payload.name, use_model=payload.use_model)


@router.post("/root-cause")
def root_cause(payload: RootCauseInput, request: Request):
    return guarded(_ai(request).root_cause, payload.entity_type, payload.ref, days=payload.days, use_model=payload.use_model)
