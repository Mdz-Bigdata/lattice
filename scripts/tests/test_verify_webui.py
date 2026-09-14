# SPDX-License-Identifier: Apache-2.0
"""Check that verification failures are reported and never skip later cleanup."""

import importlib.util
from pathlib import Path

import httpx
import pytest

SPEC = importlib.util.spec_from_file_location(
    "verify_webui", Path(__file__).resolve().parents[1] / "verify-webui.py"
)
verify_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify_module)


@pytest.mark.parametrize(
    "body", ["upstream unavailable", {"error": "unavailable"}, None]
)
def test_cleanup_continues_after_text_or_null_error(body):
    verifier = verify_module.Verification("http://127.0.0.1:8787")
    verifier.client.close()
    visited = []

    def handler(request):
        visited.append(request)
        return httpx.Response(200, json={"status": 502, "body": body})

    with httpx.Client(
        base_url="http://127.0.0.1:8787", transport=httpx.MockTransport(handler)
    ) as client:
        verifier.client = client
        verifier.later("catalog", "dropTable", path_params={})
        verifier.later("management", "deleteCatalog", path_params={})
        verifier.cleanup()
    assert len(visited) == 2
    assert len(verifier.cleanup_errors) == 2


def test_uncovered_operation_makes_verification_fail():
    verifier = verify_module.Verification("http://127.0.0.1:8787")
    try:
        verifier.advertised = {("catalog", "loadTable")}
        assert verifier.report(None)["status"] == "failed"
        assert verifier.report(None)["uncovered_operations"] == ["catalog.loadTable"]
    finally:
        verifier.client.close()
