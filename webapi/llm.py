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

"""Optional LLM provider for natural-language → SQL generation.

Settings live in a private ``llm.json`` (owner-only). A provider is picked from
``PROVIDER_CATALOG`` rather than typed by hand: every entry carries its default
endpoint, its known model ids and whether an API key is needed, so the UI can
drive the whole configuration from dropdowns. Only two client implementations
exist — the official Anthropic SDK (``protocol: "anthropic"``) and the
OpenAI-compatible ``/v1/chat/completions`` protocol (``protocol: "openai"``,
used by OpenAI, DeepSeek, 通义千问, Kimi, GLM, Ollama, vLLM …): a new provider is
a different ``base_url``, not a different client. Model ids are a convenience
list, so any model string is accepted while the provider id stays strict.

The generated SQL is *always* re-validated by the read-only guard before it is
executed; the model never receives credentials or raw data rows, and no error
message or log line ever echoes the configured API key.

The catalog is exposed to the web UI by one route, which the owner of
``webapi/app.py`` should add next to the other ``/api/llm`` routes::

    @app.get("/api/llm/catalog")
    def llm_catalog(request: Request):
        return request.app.state.llm.catalog()

``request.app.state.llm`` is the ``LlmSettings`` instance, so no extra wiring is
needed. The existing ``POST /api/llm`` and ``POST /api/llm/test`` routes keep
working unchanged.
"""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from . import env
from .connectors import SECRET_MASK

# Every provider except "anthropic" and "none" speaks the OpenAI-compatible
# chat-completions protocol, so it is the same client with another base_url.
# ``needs_base_url`` marks the providers whose endpoint the user must supply or
# confirm; for the others ``default_base_url`` is filled in automatically.
PROVIDER_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "id": "anthropic",
        "label": "Anthropic 官方 API",
        "protocol": "anthropic",
        "needs_base_url": False,
        "default_base_url": "",
        "requires_api_key": True,
        "default_model": "claude-opus-5",
        "models": [
            {"id": "claude-opus-5", "label": "Claude Opus 5（默认）"},
            {"id": "claude-sonnet-5", "label": "Claude Sonnet 5"},
            {"id": "claude-fable-5", "label": "Claude Fable 5"},
            {"id": "claude-haiku-4-5", "label": "Claude Haiku 4.5（更快）"},
        ],
        "hint": "使用官方 SDK 调用，在 console.anthropic.com 创建 API Key；地址留空即可。",
    },
    {
        "id": "openai",
        "label": "OpenAI 官方 / 兼容接口",
        "protocol": "openai",
        "needs_base_url": True,
        "default_base_url": "https://api.openai.com/v1",
        "requires_api_key": True,
        "default_model": "gpt-4o",
        "models": [
            {"id": "gpt-4o", "label": "GPT-4o（通用）"},
            {"id": "gpt-4o-mini", "label": "GPT-4o mini（轻量）"},
            {"id": "gpt-4.1", "label": "GPT-4.1"},
            {"id": "gpt-4.1-mini", "label": "GPT-4.1 mini"},
        ],
        "hint": "官方地址为 https://api.openai.com/v1；自建网关或其它兼容服务可改写该地址。",
    },
    {
        "id": "deepseek",
        "label": "DeepSeek 深度求索",
        "protocol": "openai",
        "needs_base_url": False,
        "default_base_url": "https://api.deepseek.com/v1",
        "requires_api_key": True,
        "default_model": "deepseek-chat",
        "models": [
            {"id": "deepseek-chat", "label": "DeepSeek Chat（通用对话）"},
            {"id": "deepseek-reasoner", "label": "DeepSeek Reasoner（推理增强）"},
        ],
        "hint": "在 platform.deepseek.com 申请 API Key；接口地址已内置，无需填写。",
    },
    {
        "id": "dashscope",
        "label": "阿里云百炼（通义千问）",
        "protocol": "openai",
        "needs_base_url": False,
        "default_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "requires_api_key": True,
        "default_model": "qwen-plus",
        "models": [
            {"id": "qwen-max", "label": "通义千问 Max（能力最强）"},
            {"id": "qwen-plus", "label": "通义千问 Plus（均衡）"},
            {"id": "qwen-turbo", "label": "通义千问 Turbo（更快）"},
        ],
        "hint": "使用百炼的 OpenAI 兼容模式，API Key 在阿里云百炼控制台创建。",
    },
    {
        "id": "moonshot",
        "label": "月之暗面 Kimi",
        "protocol": "openai",
        "needs_base_url": False,
        "default_base_url": "https://api.moonshot.cn/v1",
        "requires_api_key": True,
        "default_model": "moonshot-v1-32k",
        "models": [
            {"id": "moonshot-v1-8k", "label": "Kimi 8K 上下文"},
            {"id": "moonshot-v1-32k", "label": "Kimi 32K 上下文"},
            {"id": "moonshot-v1-128k", "label": "Kimi 128K 上下文"},
        ],
        "hint": "在 platform.moonshot.cn 申请 API Key；接口地址已内置，无需填写。",
    },
    {
        "id": "zhipu",
        "label": "智谱 AI（GLM）",
        "protocol": "openai",
        "needs_base_url": False,
        "default_base_url": "https://open.bigmodel.cn/api/paas/v4",
        "requires_api_key": True,
        "default_model": "glm-4-plus",
        "models": [
            {"id": "glm-4-plus", "label": "GLM-4-Plus（能力最强）"},
            {"id": "glm-4", "label": "GLM-4（通用）"},
            {"id": "glm-4-air", "label": "GLM-4-Air（轻量）"},
            {"id": "glm-4-flash", "label": "GLM-4-Flash（更快）"},
        ],
        "hint": "使用智谱开放平台的 v4 兼容接口，API Key 在 open.bigmodel.cn 创建。",
    },
    {
        "id": "ollama",
        "label": "Ollama 本地模型",
        "protocol": "openai",
        "needs_base_url": False,
        "default_base_url": "http://127.0.0.1:11434/v1",
        "requires_api_key": False,
        "default_model": "qwen2.5:7b",
        "models": [
            {"id": "qwen2.5:7b", "label": "Qwen2.5 7B"},
            {"id": "llama3.1:8b", "label": "Llama 3.1 8B"},
            {"id": "deepseek-r1:7b", "label": "DeepSeek-R1 7B"},
        ],
        "hint": "本机执行 ollama serve 即可，无需 API Key；模型名需与 ollama list 一致。",
    },
    {
        "id": "vllm",
        "label": "vLLM / 本地兼容服务",
        "protocol": "openai",
        "needs_base_url": True,
        "default_base_url": "http://127.0.0.1:8000/v1",
        "requires_api_key": False,
        "default_model": "",
        "models": [],
        "hint": "填写 vLLM、LM Studio 等服务的地址，模型名需与 --served-model-name 一致，无需 API Key。",
    },
    {
        "id": "none",
        "label": "不使用模型（内置规则）",
        "protocol": "none",
        "needs_base_url": False,
        "default_base_url": "",
        "requires_api_key": False,
        "default_model": "",
        "models": [],
        "hint": "不调用任何模型，智能问数只能回答内置示例数据的固定问题。",
    },
)

PROVIDER_INDEX: dict[str, dict[str, Any]] = {entry["id"]: entry for entry in PROVIDER_CATALOG}
PROVIDERS = frozenset(PROVIDER_INDEX)
# Model used when the stored settings leave it empty; the catalog's
# ``default_model`` is only a UI pre-selection and never applied server-side.
DEFAULT_MODELS = {"anthropic": "claude-opus-5", "openai": ""}
ANTHROPIC_FALLBACK_MODELS = ("claude-opus-5", "claude-fable-5")
MAX_QUESTION = 2000
MAX_SCHEMA_CHARS = 60_000
# Substrings every OpenAI-compatible service uses when the model id is unknown.
UNKNOWN_MODEL_HINTS = (
    "model_not_found",
    "model not found",
    "model does not exist",
    "does not exist",
    "no such model",
    "unknown model",
    "invalid model",
    "unsupported model",
    "model_not_exist",
    "模型不存在",
    "未找到模型",
)

ANTHROPIC_VERSION = "2023-06-01"

SYSTEM_PROMPT = """你是企业数据平台的 SQL 分析助手。根据用户的中文或英文问题，为指定的数据源生成一条只读查询。
规则：
1. 只生成一条 SELECT 或 WITH 查询；禁止 INSERT/UPDATE/DELETE/DDL/事务/导出/文件访问；不要以分号结尾。
2. 必须使用“目标 SQL 方言”一节指明的方言语法，不要使用其他数据库特有的函数；只能引用列出的表和列，不要虚构表或列。
3. 结果应适合作图：第一列是维度（如月份、类别、名称），随后是数值指标；聚合后按合理顺序排序；不要超过 1000 行。
4. 时间按月聚合时输出 YYYY-MM 文本；金额保留两位小数；给列起简洁的英文别名。
5. steps 用中文写 2–4 句简短的分析思路（识别指标与维度、匹配表和字段、聚合与排序方式）。
6. 输出 JSON 对象，字段：title（中文标题）、sql、steps（字符串数组）、dimension（维度列别名或空字符串）、metric（指标列别名或空字符串）、chart_type（bar/line/pie/table）。"""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "sql": {"type": "string"},
        "steps": {"type": "array", "items": {"type": "string"}},
        "dimension": {"type": "string"},
        "metric": {"type": "string"},
        "chart_type": {"type": "string", "enum": ["bar", "line", "pie", "table"]},
    },
    "required": ["title", "sql", "steps", "dimension", "metric", "chart_type"],
    "additionalProperties": False,
}


def forbidden_reason(error: Any) -> str:
    """Tell a key that may not use the model apart from a request stopped before it.

    Anthropic answers a real entitlement problem with ``permission_error``. A 403
    carrying anything else — ``forbidden`` / "Request not allowed" — comes from the
    network edge, which never saw the key: the usual cause is an outbound
    connection that does not go through the proxy the rest of the machine uses.
    """
    body = getattr(error, "body", None)
    kind = ""
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        kind = str(body["error"].get("type", "") or "")
    if kind == "permission_error":
        return "Anthropic API Key 没有访问该模型的权限。"
    return (
        "请求在到达模型前被网络出口拒绝（HTTP 403 "
        + (kind or "forbidden")
        + "），与 API Key 权限无关。"
        "请确认本机代理已开启，并用 ./start-web.sh restart 重启服务，"
        "使服务进程也走该代理（或为其设置 HTTPS_PROXY）。"
    )


class LlmError(ValueError):
    """A user-facing model/provider error."""


def _mask(value: str) -> str:
    return SECRET_MASK if value else ""


def _host(url: str) -> str:
    """Host of a URL, for an error message that names what could not be reached."""
    return urlsplit(url).hostname or url


def _discover_models(entry: dict[str, Any], data: dict[str, str]) -> set[str]:
    """Ask a provider which models it serves.

    Both protocols expose a listing endpoint: Anthropic at ``/v1/models`` with
    its own header pair, and every OpenAI-compatible server at ``{base}/models``.
    Failures are raised as ``LlmError`` with a Chinese reason and never carry the
    API key, which the caller turns into a fallback rather than a hard error.
    """
    api_key = data["api_key"]
    if entry["requires_api_key"] and not api_key:
        raise LlmError("尚未填写 API Key")
    # A provider reached over the internet may only be available through the
    # environment's proxy, which is what the Anthropic SDK already honours;
    # NO_PROXY keeps a local Ollama or vLLM server on a direct connection.
    timeout = httpx.Timeout(20, connect=8)
    if entry["protocol"] == "anthropic":
        url = (data["base_url"].rstrip("/") or "https://api.anthropic.com") + "/v1/models"
        headers = {"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION}
    else:
        # A provider that merely lets the address be overridden still has a
        # default worth listing against before the user types anything.
        base_url = (data["base_url"] or entry["default_base_url"]).rstrip("/")
        if not base_url:
            raise LlmError("尚未填写接口地址")
        url = base_url + "/models"
        headers = {"Authorization": "Bearer " + api_key} if api_key else {}
    try:
        with httpx.Client(timeout=timeout, trust_env=True) as client:
            response = client.get(url, headers=headers)
    except httpx.TimeoutException as error:
        raise LlmError(f"连接 {_host(url)} 超时，本机可能无法访问该服务商") from error
    except httpx.HTTPError as error:
        raise LlmError(
            f"无法连接 {_host(url)}：" + scrub(str(error), api_key)
        ) from error
    body = response.text[:400]
    if response.status_code == 401:
        raise LlmError("API Key 未通过验证")
    if response.status_code == 403:
        # A gateway on the way out answers 403 for a host it refuses to reach,
        # which must not be reported as the user's key being wrong.
        blocked = "not allowed" in body.lower() or "forbidden" in body.lower()
        raise LlmError(
            f"本机网络禁止访问 {_host(url)}" if blocked else "API Key 没有访问该接口的权限"
        )
    if response.status_code == 404:
        raise LlmError("该服务未提供模型列表接口")
    if response.status_code >= 400:
        raise LlmError(f"服务返回 HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as error:
        raise LlmError("模型列表不是合法 JSON") from error
    items = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise LlmError("模型列表格式无法识别")
    found = {
        str(item["id"]).strip()
        for item in items
        if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"].strip()
    }
    if not found:
        raise LlmError("服务未返回任何模型")
    return found


def provider_entry(provider: str) -> dict[str, Any]:
    """The catalog entry for a provider id; unknown ids behave like ``none``."""
    return PROVIDER_INDEX.get(provider, PROVIDER_INDEX["none"])


def scrub(text: str, api_key: str, limit: int = 200) -> str:
    """Trim a provider message and make sure it cannot carry the API key."""
    text = text.strip()
    if api_key and len(api_key) > 3:
        text = text.replace(api_key, SECRET_MASK)
    return text[:limit]


class LlmSettings:
    """Private provider configuration with environment-variable defaults."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    @staticmethod
    def catalog() -> dict[str, Any]:
        """Providers the UI offers as dropdown options (a copy; safe to mutate)."""
        return {"providers": [copy.deepcopy(entry) for entry in PROVIDER_CATALOG]}

    def _from_environment(self) -> dict[str, str]:
        provider = env("LLM_PROVIDER").strip().lower()
        if provider not in PROVIDERS:
            provider = "none"
        return {
            "provider": provider,
            "model": env("LLM_MODEL").strip(),
            "api_key": env("LLM_API_KEY").strip(),
            "base_url": env("LLM_BASE_URL").strip(),
        }

    @staticmethod
    def _complete(data: dict[str, str]) -> dict[str, str]:
        """Fill in the catalog endpoint for providers with a fixed address."""
        if data["provider"] not in PROVIDERS:
            data["provider"] = "none"
        entry = provider_entry(data["provider"])
        if not data["base_url"] and not entry["needs_base_url"]:
            data["base_url"] = entry["default_base_url"]
        return data

    def load(self) -> dict[str, str]:
        if self.path.is_file():
            try:
                data = json.loads(self.path.read_text())
            except (OSError, ValueError):
                data = {}
            if isinstance(data, dict):
                return self._complete(
                    {
                        "provider": str(data.get("provider") or "none"),
                        "model": str(data.get("model") or ""),
                        "api_key": str(data.get("api_key") or ""),
                        "base_url": str(data.get("base_url") or ""),
                    }
                )
        return self._complete(self._from_environment())

    def save(self, patch: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            current = self.load()
            provider = str(patch.get("provider") or current["provider"] or "none").lower()
            entry = PROVIDER_INDEX.get(provider)
            if entry is None:
                names = "、".join(item["id"] for item in PROVIDER_CATALOG)
                raise LlmError(f"provider 必须是模型提供方列表中的一项：{names}。")
            switched = provider != current["provider"]
            # Switching providers must not carry over another provider's model or endpoint.
            model = patch.get("model", "" if switched else current["model"])
            base_url = patch.get("base_url", "" if switched else current["base_url"])
            api_key = patch.get("api_key", "" if switched else SECRET_MASK)
            for name, value in (("model", model), ("base_url", base_url), ("api_key", api_key)):
                if value is not None and (not isinstance(value, str) or len(value) > 500 or "\n" in value):
                    raise LlmError(f"{name} 必须是不超过 500 个字符的单行文本。")
            model = (model or "").strip()
            base_url = (base_url or "").strip().rstrip("/")
            if api_key is None or api_key == SECRET_MASK:
                # The mask means "keep the stored key"; across a switch there is
                # no stored key for the new provider, so nothing is carried over.
                api_key = "" if switched else current["api_key"]
            api_key = api_key.strip()
            if not base_url and not entry["needs_base_url"]:
                base_url = entry["default_base_url"]
            if base_url:
                parsed = urlsplit(base_url)
                if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                    raise LlmError("base_url 必须是 http(s) 地址。")
            if entry["protocol"] == "openai":
                if not base_url:
                    example = entry["default_base_url"] or "http://127.0.0.1:8000/v1"
                    raise LlmError(f"{entry['label']}需要填写 base_url（例如 {example}）。")
                if not model:
                    raise LlmError(f"{entry['label']}需要选择或填写模型名称。")
            elif entry["protocol"] == "anthropic" and not model:
                model = DEFAULT_MODELS["anthropic"]
            data = {"provider": provider, "model": model, "api_key": api_key, "base_url": base_url}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", dir=self.path.parent, prefix=".llm-", delete=False
            ) as stream:
                temporary = Path(stream.name)
                os.chmod(temporary, 0o600)
                json.dump(data, stream)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.path)
            return self.status()

    def configured(self) -> bool:
        data = self.load()
        protocol = provider_entry(data["provider"])["protocol"]
        if protocol == "anthropic":
            return bool(data["api_key"] or os.environ.get("ANTHROPIC_API_KEY"))
        if protocol == "openai":
            # A key-less local server (Ollama, vLLM) is a complete configuration.
            return bool(data["base_url"] and data["model"])
        return False

    def status(self) -> dict[str, Any]:
        data = self.load()
        entry = provider_entry(data["provider"])
        configured = self.configured()
        if entry["protocol"] == "none":
            detail = "未配置模型；智能问数使用本地规则，仅支持示例数据的固定问题。"
        elif not configured:
            detail = "模型配置不完整（缺少 API Key、模型或地址）。"
        elif entry["requires_api_key"] and not data["api_key"]:
            detail = f"{entry['label']}通常需要 API Key，当前未填写，调用可能被拒绝。"
        else:
            detail = "模型已配置；自然语言问题将由模型生成 SQL，并经只读校验后执行。"
        return {
            "configured": configured,
            "provider": data["provider"],
            "provider_label": entry["label"],
            "requires_api_key": entry["requires_api_key"],
            "model": data["model"] or DEFAULT_MODELS.get(data["provider"], ""),
            "base_url": data["base_url"],
            "api_key_masked": _mask(data["api_key"]),
            "detail": detail,
        }

    def test(self) -> dict[str, Any]:
        """Round-trip one tiny request; see ``SqlGenerator.test`` for details."""
        return SqlGenerator(self).test()

    def models(self, patch: dict[str, Any] | None = None) -> dict[str, Any]:
        """List the models a provider actually offers, falling back to the catalog.

        The curated list in ``PROVIDER_CATALOG`` is only a starting point: an
        account may have more models, fewer, or entirely different ones on a
        self-hosted server. This asks the provider itself and merges what it
        answers with the curated entries, so the dropdown shows everything that
        is really available. A provider that cannot be reached is not an error
        here — the curated list is returned with a Chinese note, so choosing a
        model never depends on the network.
        """
        data = dict(self.load())
        for key in ("provider", "model", "base_url", "api_key"):
            value = (patch or {}).get(key)
            if isinstance(value, str) and value and not (key == "api_key" and value == SECRET_MASK):
                data[key] = value.strip()
        data = self._complete(data)
        entry = provider_entry(data["provider"])
        curated = [dict(item) for item in entry["models"]]
        result = {
            "provider": data["provider"],
            "provider_label": entry["label"],
            "models": curated,
            "live": False,
            "detail": "",
        }
        if entry["protocol"] == "none":
            result["detail"] = "未使用模型时无需选择模型。"
            return result
        try:
            discovered = _discover_models(entry, data)
        except LlmError as error:
            result["detail"] = f"未能从服务商读取模型列表（{error}），已显示常用模型，可手工填写模型名称。"
            return result
        known = {item["id"] for item in curated}
        merged = list(curated)
        for model_id in discovered:
            if model_id not in known:
                merged.append({"id": model_id, "label": model_id})
                known.add(model_id)
        for item in merged:
            item["available"] = item["id"] in discovered
        result["models"] = merged
        result["live"] = True
        result["detail"] = f"已从 {entry['label']} 读取 {len(discovered)} 个模型。"
        return result


def extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    try:
        data = json.loads(text)
    except ValueError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            raise LlmError("模型没有返回 JSON 结果。")
        try:
            data = json.loads(match.group(0))
        except ValueError as error:
            raise LlmError("模型返回的 JSON 无法解析。") from error
    if not isinstance(data, dict):
        raise LlmError("模型返回的结果不是 JSON 对象。")
    return data


def normalize_plan(data: dict[str, Any]) -> dict[str, Any]:
    sql = data.get("sql")
    if not isinstance(sql, str) or not sql.strip():
        raise LlmError("模型没有生成 SQL。")
    steps = data.get("steps")
    if not isinstance(steps, list):
        steps = [str(steps)] if steps else []
    steps = [str(item).strip() for item in steps if str(item).strip()][:6]
    chart = data.get("chart_type") if data.get("chart_type") in {"bar", "line", "pie", "table"} else "bar"
    return {
        "title": (str(data.get("title") or "").strip() or "查询结果")[:120],
        "sql": sql.strip().rstrip(";").strip(),
        "steps": steps,
        "dimension": str(data.get("dimension") or "").strip()[:120],
        "metric": str(data.get("metric") or "").strip()[:120],
        "chart_type": chart,
    }


def openai_failure(status_code: int, body: str, model: str, base_url: str, api_key: str) -> LlmError:
    """Name the part that failed: the key, the model, the address or the service."""
    detail = scrub(body, api_key)
    suffix = f"：{detail}" if detail else "。"
    lowered = detail.lower()
    unknown_model = any(hint in lowered for hint in UNKNOWN_MODEL_HINTS)
    if status_code in {401, 403}:
        return LlmError(f"模型接口拒绝了 API Key（HTTP {status_code}），请确认密钥正确、未过期且有该模型的权限{suffix}")
    if status_code == 404 and not unknown_model:
        return LlmError(
            f"接口地址不存在（HTTP 404）：{base_url}/chat/completions。"
            "请确认 base_url 填写正确（通常以 /v1 结尾）。"
        )
    if unknown_model:
        return LlmError(f"模型 {model} 不存在或未部署（HTTP {status_code}），请改用列表中的模型{suffix}")
    if status_code == 429:
        return LlmError(f"模型接口触发限流（HTTP 429），请稍后重试{suffix}")
    if status_code >= 500:
        return LlmError(f"模型服务内部错误（HTTP {status_code}），请检查服务端日志{suffix}")
    return LlmError(f"模型接口错误（HTTP {status_code}）{suffix}")


class SqlGenerator:
    def __init__(self, settings: LlmSettings):
        self.settings = settings

    @staticmethod
    def user_prompt(question: str, dialect_label: str, source_label: str, schema_text: str) -> str:
        return (
            f"数据源：{source_label}\n目标 SQL 方言：{dialect_label}\n"
            f"可用表与字段（库.表：列名 类型）：\n{schema_text[:MAX_SCHEMA_CHARS] or '（未能读取表结构）'}\n\n"
            f"用户问题：{question.strip()[:MAX_QUESTION]}"
        )

    def generate(self, question: str, dialect_label: str, source_label: str, schema_text: str) -> dict[str, Any]:
        data = self.settings.load()
        prompt = self.user_prompt(question, dialect_label, source_label, schema_text)
        protocol = provider_entry(data["provider"])["protocol"]
        if protocol == "anthropic":
            text = self._anthropic(data, prompt)
        elif protocol == "openai":
            text = self._openai(data, prompt)
        else:
            raise LlmError("未配置模型。")
        return normalize_plan(extract_json(text))

    def test(self) -> dict[str, Any]:
        data = self.settings.load()
        entry = provider_entry(data["provider"])
        started = time.monotonic()
        prompt = "请只回复 JSON：{\"ok\": true}"
        if entry["protocol"] == "anthropic":
            if not data["api_key"] and not os.environ.get("ANTHROPIC_API_KEY"):
                raise LlmError(f"{entry['label']}需要 API Key，请先填写后再测试。")
            text = self._anthropic(data, prompt, schema=None)
        elif entry["protocol"] == "openai":
            if not data["base_url"]:
                example = entry["default_base_url"] or "http://127.0.0.1:8000/v1"
                raise LlmError(f"{entry['label']}尚未填写 base_url（例如 {example}），无法测试。")
            if not data["model"]:
                raise LlmError(f"{entry['label']}尚未选择模型，无法测试。")
            text = self._openai(data, prompt, json_mode=False)
        else:
            raise LlmError("尚未选择模型提供方；请先在下拉列表中选择提供方并保存。")
        return {
            "ok": True,
            "detail": "模型响应正常：" + text.strip()[:80],
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
            "model": data["model"] or DEFAULT_MODELS.get(data["provider"], ""),
            "provider": data["provider"],
            "provider_label": entry["label"],
            "base_url": data["base_url"],
        }

    # ----- providers -----------------------------------------------------------------
    def _anthropic(self, data: dict[str, str], prompt: str, schema: dict | None = OUTPUT_SCHEMA) -> str:
        import anthropic

        api_key = data["api_key"] or os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise LlmError("未配置 Anthropic API Key。")
        model = data["model"] or DEFAULT_MODELS["anthropic"]
        client = anthropic.Anthropic(
            api_key=api_key,
            base_url=data["base_url"] or None,
            timeout=120.0,
            max_retries=1,
        )
        request: dict[str, Any] = {
            "model": model,
            "max_tokens": 8192,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": prompt}],
        }
        if schema is not None:
            request["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
        try:
            if not data["base_url"] and model.startswith(ANTHROPIC_FALLBACK_MODELS):
                # Server-side refusal fallback keeps analytics requests answerable.
                response = client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **request
                )
            else:
                response = client.messages.create(**request)
        except anthropic.AuthenticationError as error:
            raise LlmError("Anthropic API Key 无效或未授权。") from error
        except anthropic.PermissionDeniedError as error:
            raise LlmError(forbidden_reason(error)) from error
        except anthropic.NotFoundError as error:
            raise LlmError(f"模型 {model} 不存在或地址错误。") from error
        except anthropic.RateLimitError as error:
            raise LlmError("Anthropic 接口触发限流，请稍后重试。") from error
        except anthropic.APIStatusError as error:
            message = scrub(str(getattr(error, "message", "") or ""), api_key)
            raise LlmError(f"Anthropic 接口错误（HTTP {error.status_code}）：{message}") from error
        except anthropic.APIConnectionError as error:
            address = data["base_url"] or "api.anthropic.com"
            raise LlmError(
                f"无法连接 Anthropic 接口（{address}），请检查网络或代理：{scrub(str(error), api_key)}"
            ) from error
        if response.stop_reason == "refusal":
            raise LlmError("模型拒绝了该请求，请调整问题后重试。")
        text = "".join(block.text for block in response.content if block.type == "text")
        if not text.strip():
            raise LlmError("模型没有返回文本内容。")
        return text

    def _openai(self, data: dict[str, str], prompt: str, json_mode: bool = True) -> str:
        base_url = data["base_url"].rstrip("/")
        api_key = data["api_key"]
        if not base_url or not data["model"]:
            raise LlmError("OpenAI 兼容接口需要 base_url 和模型名称。")
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = "Bearer " + api_key
        body: dict[str, Any] = {
            "model": data["model"],
            "temperature": 0,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        url = base_url + "/chat/completions"
        try:
            with httpx.Client(timeout=httpx.Timeout(120, connect=10), trust_env=True) as client:
                response = client.post(url, headers=headers, json=body)
                if response.status_code == 400 and json_mode:
                    body.pop("response_format", None)
                    response = client.post(url, headers=headers, json=body)
        except httpx.ConnectError as error:
            raise LlmError(
                f"无法连接模型接口 {base_url}，请确认服务已启动、地址与端口可达：{scrub(str(error), api_key)}"
            ) from error
        except httpx.TimeoutException as error:
            raise LlmError(f"连接模型接口 {base_url} 超时，请检查网络或代理设置。") from error
        except httpx.HTTPError as error:
            raise LlmError(f"无法连接模型接口 {base_url}：{scrub(str(error), api_key)}") from error
        if response.status_code >= 400:
            raise openai_failure(response.status_code, response.text, data["model"], base_url, api_key)
        try:
            payload = response.json()
        except ValueError as error:
            raise LlmError(
                f"模型接口返回的不是 JSON（{base_url}），请确认地址指向兼容的 /chat/completions 服务："
                + scrub(response.text, api_key, 120)
            ) from error
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise LlmError(
                "模型接口返回的不是标准 chat/completions 响应（缺少 choices[0].message.content）："
                + scrub(json.dumps(payload, ensure_ascii=False), api_key, 120)
            ) from error
        if isinstance(content, list):
            content = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        if not isinstance(content, str) or not content.strip():
            raise LlmError("模型没有返回文本内容。")
        return content
