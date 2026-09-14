# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

import html
import importlib.util
import json
from pathlib import Path

import pytest
from jsonschema.exceptions import SchemaError


MODULE_PATH = Path(__file__).parents[1] / "fetch-salesforce-schema.py"
SPEC = importlib.util.spec_from_file_location("fetch_salesforce_schema", MODULE_PATH)
schema_tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(schema_tool)


@pytest.fixture
def schema():
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "required": ["apiName"],
        "properties": {"apiName": {"type": "string"}},
    }


def code_block(value):
    encoded = html.escape(json.dumps(value), quote=True)
    return f'<dx-code-block language="json" code-block="{encoded}"></dx-code-block>'


def test_extracts_html_attribute_and_decodes_entities(schema):
    schema["description"] = '名字 <模型> & "原样保留"'
    assert schema_tool.extract_schema(code_block(schema)) == schema


@pytest.mark.parametrize("page", ["", "<html>请登录</html>", code_block({"apiName": "demo"})])
def test_rejects_missing_schema(page):
    with pytest.raises(ValueError, match="未找到"):
        schema_tool.extract_schema(page)


def test_rejects_ambiguous_schemas(schema):
    with pytest.raises(ValueError, match="多个 Schema"):
        schema_tool.extract_schema(code_block(schema) * 2)


def test_rejects_malformed_json():
    with pytest.raises(ValueError, match="JSON 格式无效"):
        schema_tool.extract_schema('<dx-code-block language="json" code-block="{"></dx-code-block>')


def test_rejects_invalid_schema_keyword(schema):
    schema["properties"]["apiName"]["type"] = "invalid_type"
    with pytest.raises(SchemaError):
        schema_tool.extract_schema(code_block(schema))


@pytest.mark.parametrize("dialect", [{}, [], None, 7])
@pytest.mark.parametrize("existing", [True, False])
def test_invalid_schema_dialect_has_clear_error(tmp_path, schema, capsys, dialect, existing):
    schema["$schema"] = dialect
    page = tmp_path / "page.html"
    page.write_text(code_block(schema), encoding="utf-8")
    target = tmp_path / "schema.json"
    if existing:
        target.write_text(json.dumps(schema), encoding="utf-8")
    assert schema_tool.main([str(page), str(target)]) == 1
    error = capsys.readouterr().err
    assert "必须声明 JSON Schema Draft 7" in error
    assert "Traceback" not in error
    if existing:
        assert json.loads(target.read_text()) == schema
    else:
        assert not target.exists()


def test_rejects_schema_without_required_api_name(schema):
    schema["required"] = ["other"]
    with pytest.raises(ValueError, match="必填字段 apiName"):
        schema_tool.extract_schema(code_block(schema))


def test_existing_target_is_validated_and_preserved(tmp_path, schema):
    target = tmp_path / "schema.json"
    original = json.dumps(schema, indent=4).encode() + b"\n\n"
    target.write_bytes(original)
    before = target.stat()
    assert schema_tool.main([str(tmp_path / "missing.html"), str(target)]) == 0
    assert target.read_bytes() == original
    assert target.stat().st_mtime_ns == before.st_mtime_ns


def test_invalid_existing_target_is_not_replaced(tmp_path, schema, capsys):
    target = tmp_path / "schema.json"
    target.write_text("{invalid}", encoding="utf-8")
    page = tmp_path / "page.html"
    page.write_text(code_block(schema), encoding="utf-8")
    assert schema_tool.main([str(page), str(target)]) == 1
    assert target.read_text() == "{invalid}"
    assert "准备失败" in capsys.readouterr().err


def test_creates_complete_schema_in_new_directory(tmp_path, schema):
    page = tmp_path / "page.html"
    page.write_text(code_block(schema), encoding="utf-8")
    target = tmp_path / "schemas" / "salesforce.json"
    assert schema_tool.main([str(page), str(target)]) == 0
    assert json.loads(target.read_text()) == schema
    assert list(target.parent.iterdir()) == [target]


def test_atomic_publish_preserves_target_created_concurrently(tmp_path, schema):
    target = tmp_path / "schema.json"
    original = json.dumps(schema, indent=4)
    target.write_text(original, encoding="utf-8")
    replacement = {**schema, "description": "new"}
    assert schema_tool.write_schema(replacement, target) is False
    assert target.read_text() == original
    assert list(tmp_path.iterdir()) == [target]


def test_invalid_page_leaves_no_target(tmp_path):
    page = tmp_path / "page.html"
    page.write_text("<html>Unavailable</html>", encoding="utf-8")
    target = tmp_path / "schemas" / "salesforce.json"
    assert schema_tool.main([str(page), str(target)]) == 1
    assert not target.exists()
