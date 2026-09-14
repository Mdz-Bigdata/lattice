#!/usr/bin/env bash
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements. See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership. The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License. You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied. See the License for the
# specific language governing permissions and limitations
# under the License.

set -euo pipefail
ROOT="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME="$ROOT/.runtime"
QUICK=0
case "${1:-}" in
    '') ;;
    --quick) QUICK=1 ;;
    --help|-h)
        printf '%s\n' '用法：./start.sh [--quick]' \
            '准备全部组件、执行完整测试并启动 WebUI / Polaris；--quick 仅跳过测试。' \
            'macOS 可双击 start.command。首次需要网络、Go、JDK 21+、Maven 和 Node.js。' \
            '首次还会下载 StarRocks / Doris / Hive 的官方镜像（约 13 GB，需要 Docker）；' \
            '设 LATTICE_ENGINE_PULL=0 可跳过下载，这三个引擎则保持离线。' \
            '日常启动可用 ./start-web.sh；关闭用 ./start-web.sh stop。'
        exit 0 ;;
    *) printf '未知参数：%s\n' "$1" >&2; exit 2 ;;
esac
if [[ $# -gt 1 ]]; then
    printf '%s\n' '参数过多。用法：./start.sh [--quick]' >&2
    exit 2
fi

# Finder does not inherit the interactive shell's Homebrew PATH.
export PATH="${PATH:-/usr/bin:/bin}:/opt/homebrew/bin:/usr/local/bin"
# C.UTF-8, often inherited from development tools, is not a macOS locale.
if [[ "$(uname -s)" == Darwin ]]; then
    export LC_ALL=en_US.UTF-8 LC_CTYPE=en_US.UTF-8 LANG=en_US.UTF-8
fi
export PYTHONUTF8=1 PYTHONUNBUFFERED=1 UV_NO_PROGRESS=1
export UV_PYTHON_INSTALL_DIR="$RUNTIME/python"
export GOTOOLCHAIN=auto

mkdir -p "$RUNTIME/logs" "$RUNTIME/tools"
LOCK="$RUNTIME/start.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
    printf '已有启动任务或上次异常中断遗留的锁：%s\n确认没有 start.sh 运行后可删除该空目录。\n' "$LOCK" >&2
    exit 1
fi
trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
LOG="$(mktemp "$RUNTIME/logs/start-$(date +%Y%m%d-%H%M%S)-XXXXXX")"
ln -sfn "${LOG##*/}" "$RUNTIME/logs/latest.log"
trap 'status=$?; printf "启动失败（退出码 %s），日志：%s\n" "$status" "$LOG" >&2; exit "$status"' ERR

step() {
    local label="$1" status
    shift
    printf '→ %s\n' "$label"
    printf '\n=== %s ===\n' "$label" >> "$LOG"
    if "$@" >> "$LOG" 2>&1; then
        printf '  ✓ %s\n' "$label"
    else
        status=$?
        tail -n 70 "$LOG" >&2
        printf '失败：%s（退出码 %s）\n完整日志：%s\n' "$label" "$status" "$LOG" >&2
        exit "$status"
    fi
}
# The first start downloads several GB of engine images; that progress has to
# reach the terminal instead of only the log, or it looks like a hang.
stream() {
    local label="$1" status
    shift
    printf '→ %s\n' "$label"
    printf '\n=== %s ===\n' "$label" >> "$LOG"
    if "$@" 2>&1 | tee -a "$LOG"; then
        printf '  ✓ %s\n' "$label"
    else
        status=$?
        printf '失败：%s（退出码 %s）\n完整日志：%s\n' "$label" "$status" "$LOG" >&2
        exit "$status"
    fi
}
in_dir() { (cd "$1" && shift && "$@"); }
download() {
    curl --fail --location --silent --show-error --retry 3 \
        --connect-timeout 15 --max-time 180 "$1" -o "$2"
}

printf 'Lattice 一键启动（WebUI、Polaris 和命令行工具）\n日志：%s\n' "$LOG"
for tool in curl go npm; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        printf '缺少 %s，请安装并加入 PATH 后重试。详见 docs/local-start.md。\n' "$tool" >&2
        exit 1
    fi
done
# shellcheck source=scripts/java-env.sh
source "$ROOT/scripts/java-env.sh"
# Keep exported JAVA_HOME/PATH in this shell, not in a pipeline/subshell.
step '检查 JDK 21+ 和 Maven' setup_java "$RUNTIME"

# Use a pinned, project-local uv so an old system uv cannot break startup.
UV="$RUNTIME/tools/uv/uv"
if [[ ! -x "$UV" ]] || [[ "$("$UV" --version)" != 'uv 0.9.30'* ]]; then
    step '下载 uv 0.9.30 安装器' download \
        https://astral.sh/uv/0.9.30/install.sh "$RUNTIME/uv-installer.sh"
    if command -v shasum >/dev/null 2>&1; then
        installer_hash=$(shasum -a 256 "$RUNTIME/uv-installer.sh")
    else
        installer_hash=$(sha256sum "$RUNTIME/uv-installer.sh")
    fi
    if [[ "${installer_hash%% *}" != e10ae0dcd7b12acc506b3fee059f1eddd3288e277d9bdf657dd0261f574f53b4 ]]; then
        printf '%s\n' 'uv 安装器校验失败，停止执行。' >&2
        exit 1
    fi
    step '安装项目专用 uv' env UV_UNMANAGED_INSTALL="$RUNTIME/tools/uv" \
        sh "$RUNTIME/uv-installer.sh"
fi
step '准备 Python 3.12' "$UV" python install 3.12
step '安装核心库与完整校验依赖' env UV_PROJECT_ENVIRONMENT="$RUNTIME/envs/core" \
    "$UV" sync --project "$ROOT/scripts" --python 3.12 --locked
PYTHON="$RUNTIME/envs/core/bin/python"

for project in "$ROOT"/converters/*/pyproject.toml; do
    project_dir="${project%/pyproject.toml}"
    name="${project_dir##*/}"
    step "安装 Python 转换器：$name" env UV_PROJECT_ENVIRONMENT="$RUNTIME/envs/$name" \
        "$UV" sync --project "$project_dir" --python 3.12 --locked
done

SCHEMA="$ROOT/converters/salesforce/src/main/resources/schemas/salesforce-semantic-model-schema.json"
if [[ ! -f "$SCHEMA" ]]; then
    step '下载 Salesforce 官方 schema 文档' download \
        https://developer.salesforce.com/docs/data/semantic-layer/guide/salesforce-semantic-model-schema.html \
        "$RUNTIME/salesforce-schema.html"
fi
step '准备 Salesforce schema' "$PYTHON" "$ROOT/scripts/fetch-salesforce-schema.py" \
    "$RUNTIME/salesforce-schema.html" "$SCHEMA"

step '构建 Go CLI（自动选择 go.mod 要求的工具链）' in_dir "$ROOT/cli" go build -o dist/ossie .
java_goals=(clean verify)
if [[ "$QUICK" -eq 1 ]]; then java_goals=(clean package -DskipTests); fi
step '构建 Salesforce Java 转换器' in_dir "$ROOT/converters/salesforce" \
    mvn -B -ntp "${java_goals[@]}"
step '构建 Polaris Java 转换器并准备运行依赖' in_dir "$ROOT/converters/polaris" \
    mvn -B -ntp "${java_goals[@]}" \
    org.apache.maven.plugins:maven-dependency-plugin:3.8.1:copy-dependencies -DincludeScope=runtime

step '验证 TPC-DS 示例（含 SQL 校验）' "$ROOT/lattice" validate "$ROOT/examples/tpcds_semantic_model.yaml"
step '检查 Go CLI 帮助入口' "$ROOT/cli/dist/ossie" --help
for converter in dbt databricks honeydew nvidia omni orionbelt sigma snowflake wisdom; do
    step "检查命令行入口：$converter" "$ROOT/lattice" "$converter" --help
done
step '检查 GoodData API' "$ROOT/lattice" python gooddata -c 'import ossie_gooddata'
step '检查 Ontology API' "$ROOT/lattice" python ontology -c 'import ossie_ontology'
SMOKE="$(mktemp -d "$RUNTIME/smoke-XXXXXX")"
cp "$ROOT/converters/salesforce/src/test/resources/examples/ossieToSalesforce.yaml" "$SMOKE/"
cp "$ROOT/converters/salesforce/src/test/resources/examples/salesforceToOssie.json" "$SMOKE/"
step '实际执行 Apache Ossie → Salesforce 转换' "$ROOT/lattice" salesforce toSF "$SMOKE/ossieToSalesforce.yaml"
step '实际执行 Salesforce → Apache Ossie 转换' "$ROOT/lattice" salesforce toOssie "$SMOKE/salesforceToOssie.json"

if [[ "$QUICK" -eq 0 ]]; then
    step '核心 Python 测试' in_dir "$ROOT/python" "$PYTHON" -m pytest tests -q
    step '校验器回归测试' "$PYTHON" "$ROOT/validation/test_validate.py"
    step '校验器 pytest 测试' in_dir "$ROOT" "$PYTHON" -m pytest validation/tests -q
    step '启动器辅助程序测试' in_dir "$ROOT" "$PYTHON" -m pytest scripts/tests -q
    step 'Web API 与 Polaris 集成单元测试' in_dir "$ROOT" "$PYTHON" -m pytest webapi/tests integrations/polaris/test_service.py -q
    for project in "$ROOT"/converters/*/pyproject.toml; do
        project_dir="${project%/pyproject.toml}"
        name="${project_dir##*/}"
        step "Python 转换器测试：$name" in_dir "$project_dir" \
            "$RUNTIME/envs/$name/bin/python" -m pytest tests -q
    done
    step 'Go 静态检查' in_dir "$ROOT/cli" go vet ./...
    step 'Go 测试' in_dir "$ROOT/cli" go test ./...
fi

stream '启动 WebUI、Polaris、PostgreSQL、对象存储和本地示例引擎' "$ROOT/start-web.sh"
printf '\n本地工具和 WebUI 已就绪。'
if [[ "$QUICK" -eq 0 ]]; then
    printf '完整测试与运行检查通过。\n'
else
    printf '基本运行检查通过；本次按 --quick 跳过完整测试。\n'
fi
printf '使用：\n  "%s/lattice" --help\n  "%s/lattice" validate "%s/examples/tpcds_semantic_model.yaml"\n' "$ROOT" "$ROOT" "$ROOT"
printf '完整日志：%s\n' "$LOG"
printf '访问 WebUI：http://127.0.0.1:%s\n' "${LATTICE_WEB_PORT:-8787}"
printf '%s\n' '查看状态：./start-web.sh status；停止服务：./start-web.sh stop（保留数据）。'
