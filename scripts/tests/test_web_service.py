# SPDX-License-Identifier: Apache-2.0
"""Environment the launcher hands to the WebUI process."""

import importlib.util
from pathlib import Path
import sys

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

SPEC = importlib.util.spec_from_file_location("web_service", SCRIPTS / "web-service.py")
web_service = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(web_service)

SCUTIL = """<dictionary> {
  ExceptionsList : <array> {
    0 : 127.0.0.1
    1 : *.local
  }
  HTTPEnable : 1
  HTTPPort : 7897
  HTTPProxy : 127.0.0.1
  HTTPSEnable : 1
  HTTPSPort : 7897
  HTTPSProxy : 127.0.0.1
  SOCKSEnable : 1
}
"""


class Result:
    def __init__(self, stdout="", returncode=0):
        self.stdout, self.returncode = stdout, returncode


def test_the_macos_system_proxy_becomes_the_variables_httpx_reads(monkeypatch):
    """Without these, the server calls out directly and the edge answers 403."""
    monkeypatch.setattr(web_service.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(web_service.subprocess, "run", lambda *a, **k: Result(SCUTIL))
    assert web_service.system_proxy() == {
        "HTTP_PROXY": "http://127.0.0.1:7897",
        "HTTPS_PROXY": "http://127.0.0.1:7897",
    }


def test_a_disabled_or_unreadable_system_proxy_sets_nothing(monkeypatch):
    monkeypatch.setattr(web_service.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(web_service.subprocess, "run",
                        lambda *a, **k: Result(SCUTIL.replace("HTTPEnable : 1", "HTTPEnable : 0")
                                                     .replace("HTTPSEnable : 1", "HTTPSEnable : 0")))
    assert web_service.system_proxy() == {}
    monkeypatch.setattr(web_service.subprocess, "run", lambda *a, **k: Result("", returncode=1))
    assert web_service.system_proxy() == {}
    monkeypatch.setattr(web_service.platform, "system", lambda: "Linux")
    assert web_service.system_proxy() == {}


def test_an_exported_proxy_variable_wins_over_the_system_setting(monkeypatch):
    monkeypatch.setattr(web_service, "system_proxy",
                        lambda: {"HTTPS_PROXY": "http://127.0.0.1:7897"})
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")
    assert web_service.environment()["HTTPS_PROXY"] == "http://proxy.internal:3128"
    monkeypatch.delenv("HTTPS_PROXY")
    monkeypatch.delenv("https_proxy", raising=False)
    assert web_service.environment()["HTTPS_PROXY"] == "http://127.0.0.1:7897"
    # Loopback must never be proxied, or the launcher cannot reach its own services.
    assert web_service.environment()["NO_PROXY"].startswith("127.0.0.1,localhost,::1")
