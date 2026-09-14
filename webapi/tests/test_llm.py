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

"""Provider catalog, settings compatibility and connection-test diagnostics."""

import json

import httpx
import pytest

from webapi import llm as llm_module
from webapi.connectors import SECRET_MASK
from webapi.llm import PROVIDER_CATALOG, LlmError, LlmSettings, SqlGenerator

CATALOG_KEYS = {
    "id",
    "label",
    "protocol",
    "needs_base_url",
    "default_base_url",
    "requires_api_key",
    "default_model",
    "models",
    "hint",
}


REAL_CLIENT = httpx.Client


def mock_client(handler):
    """An ``httpx.Client`` replacement that answers from ``handler``.

    It derives from the client captured at import time so a second patch inside
    one test does not stack on the first one.
    """

    class FakeClient(REAL_CLIENT):
        def __init__(self, *args, **kwargs):
            kwargs.pop("trust_env", None)
            super().__init__(
                transport=httpx.MockTransport(handler),
                **{k: v for k, v in kwargs.items() if k == "timeout"},
            )

    return FakeClient


def settings_for(tmp_path, **patch):
    settings = LlmSettings(tmp_path / "llm.json")
    settings.save(patch)
    return settings


# ----- provider catalog --------------------------------------------------------------------
def test_catalog_covers_every_provider_with_complete_entries():
    catalog = LlmSettings.catalog()
    providers = catalog["providers"]
    assert [item["id"] for item in providers] == [
        "anthropic",
        "openai",
        "deepseek",
        "dashscope",
        "moonshot",
        "zhipu",
        "ollama",
        "vllm",
        "none",
    ]
    for entry in providers:
        assert set(entry) == CATALOG_KEYS
        assert entry["label"] and entry["hint"]
        assert entry["protocol"] in {"anthropic", "openai", "none"}
        assert isinstance(entry["needs_base_url"], bool)
        assert isinstance(entry["requires_api_key"], bool)
        for model in entry["models"]:
            assert set(model) == {"id", "label"} and model["id"] and model["label"]
        assert not entry["default_model"] or entry["default_model"] in {
            model["id"] for model in entry["models"]
        }
    by_id = {entry["id"]: entry for entry in providers}
    # Everything but Anthropic and "none" is the same OpenAI-compatible client.
    assert {entry["id"] for entry in providers if entry["protocol"] == "openai"} == {
        "openai",
        "deepseek",
        "dashscope",
        "moonshot",
        "zhipu",
        "ollama",
        "vllm",
    }
    assert by_id["anthropic"]["default_model"] == "claude-opus-5"
    assert by_id["deepseek"]["default_base_url"] == "https://api.deepseek.com/v1"
    assert by_id["dashscope"]["default_base_url"] == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert by_id["moonshot"]["default_base_url"] == "https://api.moonshot.cn/v1"
    assert by_id["zhipu"]["default_base_url"] == "https://open.bigmodel.cn/api/paas/v4"
    assert by_id["ollama"]["default_base_url"] == "http://127.0.0.1:11434/v1"
    assert by_id["ollama"]["requires_api_key"] is False
    assert by_id["vllm"]["requires_api_key"] is False and by_id["vllm"]["needs_base_url"] is True
    assert by_id["openai"]["default_base_url"] == "https://api.openai.com/v1"


def test_catalog_hands_out_a_copy():
    catalog = LlmSettings.catalog()
    catalog["providers"][0]["models"].clear()
    assert PROVIDER_CATALOG[0]["models"], "the module-level catalog must stay intact"
    assert LlmSettings.catalog()["providers"][0]["models"]


# ----- settings ----------------------------------------------------------------------------
def test_hosted_provider_fills_in_its_endpoint(tmp_path):
    settings = LlmSettings(tmp_path / "llm.json")
    status = settings.save({"provider": "deepseek", "model": "deepseek-chat", "api_key": "sk-ds"})
    assert status["configured"] is True
    assert status["base_url"] == "https://api.deepseek.com/v1"
    assert status["provider_label"] == "DeepSeek 深度求索"
    assert status["api_key_masked"] == SECRET_MASK
    assert "sk-ds" not in json.dumps(status, ensure_ascii=False)
    # An explicit endpoint (gateway or proxy) still wins.
    assert settings.save({"base_url": "https://gateway.example.com/v1"})["base_url"] == (
        "https://gateway.example.com/v1"
    )


def test_local_provider_is_configurable_without_an_api_key(tmp_path):
    settings = LlmSettings(tmp_path / "llm.json")
    status = settings.save({"provider": "ollama", "model": "qwen2.5:7b"})
    assert status["configured"] is True and status["api_key_masked"] == ""
    assert status["base_url"] == "http://127.0.0.1:11434/v1"
    assert status["requires_api_key"] is False
    vllm = settings.save({"provider": "vllm", "model": "Qwen2.5-7B-Instruct", "base_url": "http://127.0.0.1:8000/v1"})
    assert vllm["configured"] is True and vllm["api_key_masked"] == ""
    with pytest.raises(LlmError, match="base_url"):
        LlmSettings(tmp_path / "other.json").save({"provider": "vllm", "model": "m"})


def test_mask_keeps_the_stored_key_and_switching_provider_drops_it(tmp_path):
    settings = settings_for(tmp_path, provider="moonshot", model="moonshot-v1-32k", api_key="sk-moon")
    settings.save({"provider": "moonshot", "model": "moonshot-v1-128k", "api_key": SECRET_MASK})
    assert settings.load()["api_key"] == "sk-moon"
    assert settings.load()["model"] == "moonshot-v1-128k"
    # Another provider must never inherit this key.
    settings.save({"provider": "zhipu", "model": "glm-4", "api_key": SECRET_MASK})
    assert settings.load()["api_key"] == ""


def test_provider_id_is_strict_and_model_string_is_permissive(tmp_path):
    settings = LlmSettings(tmp_path / "llm.json")
    with pytest.raises(LlmError, match="provider"):
        settings.save({"provider": "azure-openai", "model": "gpt-4o"})
    with pytest.raises(LlmError, match="模型"):
        settings.save({"provider": "dashscope"})
    status = settings.save({"provider": "dashscope", "model": "qwen3-coder-plus-2025-07-22", "api_key": "sk-q"})
    assert status["model"] == "qwen3-coder-plus-2025-07-22" and status["configured"] is True


def test_status_flags_a_hosted_provider_without_a_key(tmp_path):
    settings = settings_for(tmp_path, provider="zhipu", model="glm-4")
    status = settings.status()
    assert status["configured"] is True and "API Key" in status["detail"]


def test_environment_defaults_use_the_catalog_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("LATTICE_LLM_PROVIDER", "moonshot")
    monkeypatch.setenv("LATTICE_LLM_MODEL", "moonshot-v1-8k")
    monkeypatch.setenv("LATTICE_LLM_API_KEY", "sk-env")
    monkeypatch.delenv("LATTICE_LLM_BASE_URL", raising=False)
    settings = LlmSettings(tmp_path / "missing.json")
    assert settings.load()["base_url"] == "https://api.moonshot.cn/v1"
    assert settings.configured() is True


def test_the_environment_overrides_a_stored_provider(tmp_path, monkeypatch):
    """Only the ``LATTICE_*`` names are read; the saved file is the fallback."""
    for suffix in ("PROVIDER", "MODEL", "API_KEY", "BASE_URL"):
        monkeypatch.delenv(f"LATTICE_LLM_{suffix}", raising=False)
    monkeypatch.setenv("LATTICE_LLM_PROVIDER", "moonshot")
    monkeypatch.setenv("LATTICE_LLM_MODEL", "moonshot-v1-8k")
    monkeypatch.setenv("LATTICE_LLM_API_KEY", "sk-env")
    assert LlmSettings(tmp_path / "missing.json").load()["base_url"] == "https://api.moonshot.cn/v1"
    monkeypatch.setenv("LATTICE_LLM_PROVIDER", "zhipu")
    assert LlmSettings(tmp_path / "missing.json").load()["provider"] == "zhipu"


# ----- backward compatibility ---------------------------------------------------------------
def test_settings_written_before_the_catalog_still_load(tmp_path):
    path = tmp_path / "llm.json"
    path.write_text(json.dumps({"provider": "openai", "model": "m", "base_url": "https://api.example.com/v1", "api_key": "k"}))
    settings = LlmSettings(path)
    assert settings.configured() is True
    assert settings.status()["model"] == "m" and settings.status()["base_url"] == "https://api.example.com/v1"
    # The legacy "openai" id keeps requiring an explicit endpoint and model.
    with pytest.raises(LlmError):
        LlmSettings(tmp_path / "fresh.json").save({"provider": "openai", "model": "m"})
    with pytest.raises(LlmError):
        LlmSettings(tmp_path / "fresh.json").save({"provider": "openai", "base_url": "https://api.openai.com/v1"})
    assert LlmSettings(tmp_path / "fresh.json").save({"provider": "anthropic", "api_key": "k"})["model"] == "claude-opus-5"
    assert LlmSettings(tmp_path / "fresh.json").save({"provider": "none"})["configured"] is False


def test_unreadable_or_unknown_provider_falls_back_to_none(tmp_path):
    path = tmp_path / "llm.json"
    path.write_text(json.dumps({"provider": "gemini", "model": "x"}))
    settings = LlmSettings(path)
    assert settings.load()["provider"] == "none" and settings.configured() is False
    path.write_text("{not json")
    assert settings.status()["configured"] is False


# ----- connection test diagnostics -----------------------------------------------------------
def test_connection_test_reports_success(tmp_path, monkeypatch):
    settings = settings_for(tmp_path, provider="deepseek", model="deepseek-chat", api_key="sk-ds")

    def handler(request):
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer sk-ds"
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]})

    monkeypatch.setattr(llm_module.httpx, "Client", mock_client(handler))
    result = settings.test()
    assert result["ok"] is True and result["provider"] == "deepseek"
    assert result["model"] == "deepseek-chat" and result["latency_ms"] >= 0
    assert "sk-ds" not in json.dumps(result, ensure_ascii=False)


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (httpx.Response(401, json={"error": {"message": "Invalid authentication token sk-secret"}}), "API Key"),
        (httpx.Response(403, text="forbidden"), "API Key"),
        (httpx.Response(404, text="<html>404 Not Found</html>"), "接口地址不存在"),
        (httpx.Response(400, json={"error": {"message": "The model `qwen-max` does not exist"}}), "不存在或未部署"),
        (httpx.Response(429, text="rate limited"), "限流"),
        (httpx.Response(503, text="upstream down"), "模型服务内部错误"),
        (httpx.Response(200, text="<html>hello</html>"), "不是 JSON"),
        (httpx.Response(200, json={"id": "x"}), "标准 chat/completions"),
    ],
)
def test_connection_test_names_the_failing_part(tmp_path, monkeypatch, response, expected):
    settings = settings_for(tmp_path, provider="dashscope", model="qwen-max", api_key="sk-secret")
    monkeypatch.setattr(llm_module.httpx, "Client", mock_client(lambda request: response))
    with pytest.raises(LlmError) as error:
        settings.test()
    message = str(error.value)
    assert expected in message
    assert "sk-secret" not in message, "the API key must never reach a user-facing message"


def test_connection_test_reports_an_unreachable_endpoint(tmp_path, monkeypatch):
    settings = settings_for(tmp_path, provider="ollama", model="qwen2.5:7b")

    def handler(request):
        raise httpx.ConnectError("[Errno 61] Connection refused", request=request)

    monkeypatch.setattr(llm_module.httpx, "Client", mock_client(handler))
    with pytest.raises(LlmError, match="无法连接模型接口 http://127.0.0.1:11434/v1"):
        settings.test()

    def slow(request):
        raise httpx.ConnectTimeout("timed out", request=request)

    monkeypatch.setattr(llm_module.httpx, "Client", mock_client(slow))
    with pytest.raises(LlmError, match="超时"):
        SqlGenerator(settings).test()


def test_connection_test_without_configuration_explains_what_is_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    settings = LlmSettings(tmp_path / "llm.json")
    with pytest.raises(LlmError, match="模型提供方"):
        settings.test()
    settings.path.write_text(json.dumps({"provider": "openai", "base_url": "", "model": "gpt-4o"}))
    with pytest.raises(LlmError, match="base_url"):
        settings.test()
    settings.path.write_text(json.dumps({"provider": "anthropic", "model": "claude-opus-5"}))
    with pytest.raises(LlmError, match="API Key"):
        settings.test()


def test_model_discovery_merges_live_models_with_the_curated_list(monkeypatch, tmp_path):
    """The curated list is a starting point; an account may serve other models."""
    import httpx

    from webapi.llm import LlmSettings

    def handler(request):
        assert request.url.path.endswith("/models")
        assert request.headers["Authorization"] == "Bearer real-key"
        return httpx.Response(
            200,
            json={"data": [{"id": "deepseek-chat"}, {"id": "deepseek-vl-7b"}]},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(llm_module.httpx, "Client", lambda **kwargs: client)
    settings = LlmSettings(tmp_path / "llm.json")
    result = settings.models({"provider": "deepseek", "api_key": "real-key"})
    assert result["live"] is True
    ids = [item["id"] for item in result["models"]]
    assert "deepseek-chat" in ids, "a curated model the provider confirms stays"
    assert "deepseek-vl-7b" in ids, "a model only the provider knows is added"
    assert result["models"][ids.index("deepseek-vl-7b")]["available"] is True
    assert "读取 2 个模型" in result["detail"]


@pytest.mark.parametrize(
    "status, body, expected",
    [
        (401, "{}", "API Key 未通过验证"),
        (403, '{"error":{"message":"Request not allowed"}}', "本机网络禁止访问"),
        (403, "{}", "没有访问该接口的权限"),
        (404, "{}", "未提供模型列表接口"),
        (500, "{}", "HTTP 500"),
    ],
)
def test_model_discovery_names_the_real_cause(monkeypatch, tmp_path, status, body, expected):
    """A blocked network must never be reported as the user's key being wrong."""
    import httpx

    from webapi.llm import LlmSettings

    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, text=body))
    )
    monkeypatch.setattr(llm_module.httpx, "Client", lambda **kwargs: client)
    result = LlmSettings(tmp_path / "llm.json").models(
        {"provider": "deepseek", "api_key": "k"}
    )
    assert result["live"] is False
    assert expected in result["detail"]
    # The dropdown must still be usable when discovery fails.
    assert result["models"], "the curated list is the offline fallback"


def test_model_discovery_never_echoes_the_api_key(monkeypatch, tmp_path):
    import httpx

    from webapi.llm import LlmSettings

    secret = "sk-super-secret-value"

    def explode(request):
        raise httpx.ConnectError(f"failed talking to host with {secret}")

    client = httpx.Client(transport=httpx.MockTransport(explode))
    monkeypatch.setattr(llm_module.httpx, "Client", lambda **kwargs: client)
    result = LlmSettings(tmp_path / "llm.json").models(
        {"provider": "deepseek", "api_key": secret}
    )
    assert secret not in json.dumps(result, ensure_ascii=False)


def test_provider_calls_honour_the_environment_proxy(monkeypatch, tmp_path):
    """A provider may only be reachable through the environment's proxy, which is
    what the Anthropic SDK already uses; disabling it made model discovery and
    OpenAI-compatible chat fail on exactly the networks that need it."""
    import httpx

    from webapi.llm import LlmSettings

    seen = {}
    real_client = httpx.Client

    def record(**kwargs):
        seen.setdefault("trust_env", []).append(kwargs.get("trust_env"))
        return real_client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"data": [{"id": "m-1"}]})
            )
        )

    monkeypatch.setattr(llm_module.httpx, "Client", record)
    result = LlmSettings(tmp_path / "llm.json").models(
        {"provider": "deepseek", "api_key": "k"}
    )
    assert result["live"] is True
    assert seen["trust_env"] == [True], "the proxy must not be bypassed"


# ----- 403 diagnosis ----------------------------------------------------------------
def test_a_403_from_the_network_edge_is_not_blamed_on_the_api_key():
    """The edge never sees the key, so "key has no access" sends the reader hunting."""

    class Error:
        body = {"error": {"type": "forbidden", "message": "Request not allowed"}}

    message = llm_module.forbidden_reason(Error())
    assert "API Key" not in message.split("与 API Key 权限无关")[0]
    assert "代理" in message and "HTTPS_PROXY" in message


def test_a_real_entitlement_403_still_names_the_api_key():
    class Error:
        body = {"error": {"type": "permission_error", "message": "not allowed"}}

    assert llm_module.forbidden_reason(Error()) == "Anthropic API Key 没有访问该模型的权限。"


def test_an_unparsable_403_body_does_not_crash_the_handler():
    class Error:
        body = None

    assert "403" in llm_module.forbidden_reason(Error())
