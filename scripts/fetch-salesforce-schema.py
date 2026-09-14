#!/usr/bin/env python3
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

"""Extract the Salesforce schema from an already downloaded official page."""

import argparse
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import sys
import tempfile

from jsonschema import Draft7Validator
from jsonschema.exceptions import SchemaError


class SchemaBlocks(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "dx-code-block":
            return
        attributes = dict(attrs)
        if (attributes.get("language") or "").lower() == "json":
            block = attributes.get("code-block")
            if block is None:
                raise ValueError("Salesforce JSON 代码块缺少 code-block 内容")
            self.blocks.append(block)


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON 包含重复字段：{key}")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError(f"JSON 包含非法常量：{value}")


def parse_json(content: str) -> object:
    try:
        return json.loads(
            content, object_pairs_hook=_unique_object, parse_constant=_invalid_constant
        )
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON 格式无效（第 {exc.lineno} 行，第 {exc.colno} 列）") from exc


def validate_schema(schema: object) -> dict:
    if not isinstance(schema, dict):
        raise ValueError("Salesforce Schema 必须是 JSON 对象")
    dialect = schema.get("$schema")
    if not isinstance(dialect, str) or dialect not in {
        "http://json-schema.org/draft-07/schema#",
        "https://json-schema.org/draft-07/schema#",
    }:
        raise ValueError("Salesforce Schema 必须声明 JSON Schema Draft 7")
    Draft7Validator.check_schema(schema)
    if "apiName" not in schema.get("required", []):
        raise ValueError("Salesforce Schema 缺少必填字段 apiName")
    if "apiName" not in schema.get("properties", {}):
        raise ValueError("Salesforce Schema 缺少 apiName 字段定义")
    return schema


def extract_schema(html: str) -> dict:
    parser = SchemaBlocks()
    parser.feed(html)
    parser.close()
    candidates = []
    for block in parser.blocks:
        document = parse_json(block)
        if isinstance(document, dict) and "$schema" in document:
            candidates.append(validate_schema(document))
    if not candidates:
        raise ValueError("页面中未找到 Salesforce Semantic Model JSON Schema")
    if len(candidates) != 1:
        raise ValueError("页面包含多个 Schema，无法确定 Salesforce Schema")
    return candidates[0]


def read_schema(path: Path) -> dict:
    return validate_schema(parse_json(path.read_text(encoding="utf-8")))


def write_schema(schema: dict, target: Path) -> bool:
    """Publish a complete file atomically, preserving any existing target."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=target.parent, prefix=".salesforce-", delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(schema, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            # A hard link publishes the complete file without overwriting a target
            # created concurrently by another startup process.
            os.link(temporary, target)
        except FileExistsError:
            read_schema(target)
            return False
        return True
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class ChineseArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(2, f"参数错误：{message}\n")


def main(argv: list[str] | None = None) -> int:
    parser = ChineseArgumentParser(description="从 Salesforce 官方页面 HTML 提取并验证 Schema")
    parser.add_argument("html", type=Path, help="已经下载的官方页面 HTML 文件")
    parser.add_argument("target", type=Path, help="目标 JSON Schema 文件")
    args = parser.parse_args(argv)
    try:
        if args.target.exists() or args.target.is_symlink():
            read_schema(args.target)
            print(f"已验证现有 Salesforce Schema，保留原文件：{args.target}")
            return 0
        schema = extract_schema(args.html.read_text(encoding="utf-8"))
        created = write_schema(schema, args.target)
        if created:
            print(f"Salesforce Schema 已提取并验证：{args.target}")
        else:
            print(f"已验证现有 Salesforce Schema，保留原文件：{args.target}")
        return 0
    except (OSError, UnicodeError, ValueError, SchemaError) as exc:
        print(f"Salesforce Schema 准备失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
