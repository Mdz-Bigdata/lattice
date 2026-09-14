#!/usr/bin/env bash
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements. See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership. The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License. You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied. See the License for the
# specific language governing permissions and limitations
# under the License.

set -euo pipefail
PROJECT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
ACTION="${1:-start}"
if [[ $# -gt 1 ]]; then
    printf '%s\n' '用法：./start-web.sh [start|stop|restart|status|engines]' >&2
    exit 2
fi
case "$ACTION" in
    start|stop|restart|status|engines) ;;
    --help|-h)
        printf '%s\n' '用法：./start-web.sh [start|stop|restart|status|engines]' \
            '默认 start：安装锁定依赖、构建 WebUI、启动 Polaris、本地 Web 服务和全部示例引擎。' \
            '首次启动会下载 StarRocks、Doris、Hive 的官方镜像（约 13 GB，需要 Docker）；' \
            '设 LATTICE_ENGINE_PULL=0 可跳过下载。engines：只启动本地示例引擎。' \
            '默认地址：http://127.0.0.1:8787；可通过 LATTICE_WEB_PORT 设置端口。' \
            'stop 保留全部本地数据库文件。首次请先运行 ./start.sh。'
        exit 0 ;;
    *) printf '未知操作：%s\n' "$ACTION" >&2; exit 2 ;;
esac

export PATH="${PATH:-/usr/bin:/bin}:/opt/homebrew/bin:/usr/local/bin:/usr/sbin"
export PYTHONUTF8=1 PYTHONUNBUFFERED=1 UV_NO_PROGRESS=1
export UV_PYTHON_INSTALL_DIR="$PROJECT_DIR/.runtime/python"
if [[ "$(uname -s)" == Darwin ]]; then
    export LC_ALL=en_US.UTF-8 LC_CTYPE=en_US.UTF-8 LANG=en_US.UTF-8
fi
if [[ "$ACTION" == start || "$ACTION" == restart || "$ACTION" == engines ]]; then
    if [[ ! -x "$PROJECT_DIR/.runtime/tools/uv/uv" ]]; then
        printf '%s\n' '尚未安装项目专用 uv。请先运行 ./start.sh，再启动 WebUI。' >&2
        exit 1
    fi
fi

PYTHON_CMD="$PROJECT_DIR/.runtime/envs/core/bin/python"
if [[ ! -x "$PYTHON_CMD" ]]; then
    if ! command -v python3 >/dev/null 2>&1; then
        printf '%s\n' '未找到 Python。请先运行 ./start.sh 准备项目环境。' >&2
        exit 1
    fi
    PYTHON_CMD="$(command -v python3)"
fi
# The Python helper holds one lock across uv sync, npm ci/build, and startup.
# This also keeps concurrent double-clicks from changing dependencies together.
exec "$PYTHON_CMD" "$PROJECT_DIR/scripts/web-service.py" "$ACTION"
