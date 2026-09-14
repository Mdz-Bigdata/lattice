#!/usr/bin/env python3
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

"""Locked preparation and conservative lifecycle control for the local WebUI."""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / ".runtime" / "webui"
CORE_PYTHON = ROOT / ".runtime" / "envs" / "core" / "bin" / "python"
UV = ROOT / ".runtime" / "tools" / "uv" / "uv"
PID_FILE = RUNTIME / "server.pid.json"
SERVER_LOG = RUNTIME / "server.log"
STARTUP_LOG = RUNTIME / "startup.log"
POLARIS = ROOT / "scripts" / "polaris-service.sh"
APPLICATION = "lattice-webui"


def _env(name: str, default: str = "") -> str:
    """Read the LATTICE_<name> environment variable."""
    return os.environ.get(f"LATTICE_{name}", default)


def say(message: str) -> None:
    print(message, flush=True)


def launch_command(port: int) -> list[str]:
    return [
        str(CORE_PYTHON),
        "-m",
        "uvicorn",
        "webapi.app:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]


def process_identity(pid: int) -> str:
    if pid <= 1:
        return ""
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "lstart=", "-o", "command="],
        text=True,
        capture_output=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def process_in_project(pid: int) -> bool:
    result = subprocess.run(
        ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
        text=True,
        capture_output=True,
        check=False,
    )
    return any(line == "n" + str(ROOT) for line in result.stdout.splitlines())


def listener_pids(port: int) -> set[int]:
    result = subprocess.run(
        ["lsof", "-nP", "-a", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError("无法检查端口进程：" + result.stderr.strip())
    return {int(value) for value in result.stdout.split() if value.isdigit()}


def free_port(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        # Match uvicorn's restart behavior when old HTTP connections are in TIME_WAIT.
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            connection.bind(("127.0.0.1", port))
        except OSError as error:
            raise RuntimeError(
                f"端口 {port} 已被其他进程占用，未启动或终止任何服务。"
            ) from error


def read_state() -> dict | None:
    if not PID_FILE.exists():
        return None
    try:
        state = json.loads(PID_FILE.read_text())
        valid = (
            isinstance(state, dict)
            and type(state.get("pid")) is int
            and type(state.get("port")) is int
            and isinstance(state.get("identity"), str)
            and isinstance(state.get("instance_id"), str)
            and state.get("root") == str(ROOT)
        )
        if not valid:
            raise ValueError("invalid PID record")
        return state
    except (OSError, ValueError, TypeError) as error:
        raise RuntimeError(
            f"PID 记录无效，无法安全管理进程：{PID_FILE}。请检查该文件。"
        ) from error


def owns(state: dict) -> bool:
    actual = process_identity(state["pid"])
    expected_command = " ".join(launch_command(state["port"]))
    return bool(
        actual
        and actual == state["identity"]
        and actual.endswith(expected_command)
        and process_in_project(state["pid"])
    )


def clean_stale_state(state: dict | None) -> dict | None:
    if state and not owns(state):
        say("已清理过期的 WebUI PID 记录；未终止任何无关进程。")
        PID_FILE.unlink(missing_ok=True)
        return None
    return state


def health(port: int, instance_id: str | None = None) -> dict:
    # Never route loopback lifecycle checks through a user's HTTP proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{port}/api/health", timeout=4) as response:
        data = json.loads(response.read(1024 * 1024))
    if (
        not isinstance(data, dict)
        or data.get("application") != APPLICATION
        or data.get("status") != "ok"
    ):
        raise RuntimeError("端口上的服务不是当前 Lattice WebUI。")
    if instance_id is not None and data.get("instance_id") != instance_id:
        raise RuntimeError("WebUI 实例标识不匹配，拒绝接管其他进程。")
    if not isinstance(data.get("services"), dict) or not isinstance(
        data["services"].get("polaris"), dict
    ):
        raise RuntimeError("WebUI 健康响应缺少 Polaris 服务状态。")
    return data


def print_ready(port: int, managed: bool = True) -> None:
    say(f"WebUI 已就绪：http://127.0.0.1:{port}")
    say(f"服务日志：{SERVER_LOG}")
    if not managed:
        say("正在复用同一项目手动启动的服务；未接管 PID，stop 不会终止该进程。")


def existing_service(port: int) -> bool:
    state = clean_stale_state(read_state())
    if state:
        if state["port"] != port:
            raise RuntimeError(
                f"本项目已在端口 {state['port']} 运行。请先执行 ./start-web.sh stop。"
            )
        data = health(port, state["instance_id"])
        if data["services"]["polaris"].get("status") != "online":
            raise RuntimeError(
                "WebUI 正在运行，但 Polaris 未连接。请执行 ./start-web.sh restart。"
            )
        if listener_pids(port) != {state["pid"]}:
            raise RuntimeError("WebUI 监听端口的进程与 PID 记录不一致，拒绝继续。")
        print_ready(port)
        return True
    listeners = listener_pids(port)
    if listeners:
        try:
            data = health(port)
            expected = " ".join(launch_command(port))
            relative = " ".join(
                [str(CORE_PYTHON.relative_to(ROOT)), *launch_command(port)[1:]]
            )
            accepted = (" " + expected, " " + relative, " ./" + relative)
            if any(
                not process_identity(pid).endswith(accepted)
                or not process_in_project(pid)
                for pid in listeners
            ):
                raise RuntimeError("监听进程不属于本项目。")
            if data["services"]["polaris"].get("status") != "online":
                raise RuntimeError("手动启动的 WebUI 尚未连接 Polaris。")
        except (OSError, ValueError, RuntimeError) as error:
            raise RuntimeError(
                f"端口 {port} 已被其他服务占用，未启动或终止任何进程。{error}"
            ) from error
        print_ready(port, managed=False)
        return True
    free_port(port)
    return False


def tail(path: Path, lines: int = 45) -> None:
    if path.exists():
        print(
            "\n".join(path.read_text(errors="replace").splitlines()[-lines:]),
            file=sys.stderr,
        )


def system_proxy() -> dict[str, str]:
    """The macOS proxy setting, as the environment variables httpx understands.

    macOS keeps the proxy in its network configuration, and httpx — which every
    provider in webapi/llm.py uses — reads only HTTP_PROXY / HTTPS_PROXY. A server
    started from Finder, or from a shell without those variables, therefore calls
    api.anthropic.com directly while the browser beside it goes through the proxy,
    and the network edge answers 403. Same gap as the Homebrew PATH line in start.sh.
    """
    if platform.system() != "Darwin":
        return {}
    result = subprocess.run(["scutil", "--proxy"], text=True, capture_output=True, check=False)
    if result.returncode:
        return {}
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition(" : ")
        if separator:
            values[key.strip()] = value.strip()
    proxies = {}
    for name, prefix in (("HTTP_PROXY", "HTTP"), ("HTTPS_PROXY", "HTTPS")):
        host, port = values.get(prefix + "Proxy", ""), values.get(prefix + "Port", "")
        # Both are CONNECT proxies reached over http://, whatever they forward.
        if values.get(prefix + "Enable") == "1" and host and port.isdigit():
            proxies[name] = f"http://{host}:{port}"
    return proxies


def environment() -> dict[str, str]:
    result = os.environ.copy()
    result["LATTICE_PYTHON"] = str(CORE_PYTHON)
    result["UV_PROJECT_ENVIRONMENT"] = str(CORE_PYTHON.parent.parent)
    result["UV_PYTHON_INSTALL_DIR"] = str(ROOT / ".runtime" / "python")
    result["PYTHONUNBUFFERED"] = "1"
    result["PYTHONUTF8"] = "1"
    for name, value in system_proxy().items():
        # An explicitly exported variable always wins over the system setting.
        if not result.get(name) and not result.get(name.lower()):
            result[name] = value
    result["NO_PROXY"] = "127.0.0.1,localhost,::1" + (
        "," + result["NO_PROXY"] if result.get("NO_PROXY") else ""
    )
    return result


def run_step(label: str, command: list[str], cwd: Path = ROOT) -> None:
    say("→ " + label)
    with STARTUP_LOG.open("a") as log:
        log.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} {label} ===\n")
        log.flush()
        result = subprocess.run(
            command,
            cwd=cwd,
            env=environment(),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
        )
    if result.returncode:
        tail(STARTUP_LOG)
        raise RuntimeError(
            f"{label}失败（退出码 {result.returncode}），日志：{STARTUP_LOG}"
        )
    say("  ✓ " + label)


def stop_backend(state: dict | None) -> None:
    if not state:
        say("没有由启动器管理的 WebUI 进程。")
        return
    if not owns(state):
        PID_FILE.unlink(missing_ok=True)
        say("WebUI PID 已过期；未终止任何无关进程。")
        return
    os.kill(state["pid"], signal.SIGTERM)
    for _ in range(100):
        if not owns(state):
            PID_FILE.unlink(missing_ok=True)
            say("WebUI 服务已停止，数据文件已保留。")
            return
        time.sleep(0.2)
    raise RuntimeError("WebUI 未在 20 秒内停止。拒绝强制终止进程；请检查服务日志。")


def start(port: int) -> None:
    if existing_service(port):
        return
    if not UV.is_file() or not os.access(UV, os.X_OK):
        raise RuntimeError("尚未安装项目专用 uv，请先运行 ./start.sh。")
    if not shutil.which("npm"):
        raise RuntimeError("未找到 npm，请安装 Node.js 并加入 PATH 后重试。")
    run_step(
        "安装 Web API 锁定依赖",
        [
            str(UV),
            "sync",
            "--project",
            str(ROOT / "scripts"),
            "--python",
            "3.12",
            "--locked",
        ],
    )
    run_step(
        "安装 WebUI 锁定依赖", ["npm", "ci", "--no-audit", "--no-fund"], ROOT / "web"
    )
    run_step("构建 WebUI", ["npm", "run", "build"], ROOT / "web")
    if not (ROOT / "web" / "dist" / "index.html").is_file():
        raise RuntimeError("WebUI 构建未生成 dist/index.html。")
    # Detect races with externally launched processes before starting dependencies.
    free_port(port)
    receipt = RUNTIME / f"polaris-start-{uuid.uuid4().hex}.json"
    process = None
    state = None
    try:
        run_step(
            "启动并检查 Apache Polaris",
            [str(POLARIS), "start", "--receipt", str(receipt)],
        )
        if not receipt.is_file():
            raise RuntimeError("Polaris 未返回启动回执，无法验证新启动组件的归属。")
        free_port(port)
        instance_id = uuid.uuid4().hex
        child_env = environment()
        child_env["LATTICE_WEB_INSTANCE_ID"] = instance_id
        child_env["LATTICE_WEB_RUNTIME"] = str(RUNTIME)
        with SERVER_LOG.open("a") as log:
            log.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} WebUI 启动 ===\n")
            log.flush()
            process = subprocess.Popen(
                launch_command(port),
                cwd=ROOT,
                env=child_env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
        identity = process_identity(process.pid)
        if not identity:
            raise RuntimeError("WebUI 进程未成功启动。")
        state = {
            "pid": process.pid,
            "identity": identity,
            "port": port,
            "instance_id": instance_id,
            "root": str(ROOT),
        }
        temporary = PID_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps(state))
        temporary.chmod(0o600)
        temporary.replace(PID_FILE)
        last_error = "服务尚未响应"
        for _ in range(60):
            if process.poll() is not None:
                raise RuntimeError(f"WebUI 提前退出（退出码 {process.returncode}）。")
            try:
                data = health(port, instance_id)
                if not owns(state) or listener_pids(port) != {process.pid}:
                    raise RuntimeError("WebUI 端口所有权与新启动进程不一致。")
                if data["services"]["polaris"].get("status") != "online":
                    raise RuntimeError("Polaris 服务尚未连接。")
                receipt.unlink(missing_ok=True)
                print_ready(port)
                return
            except (OSError, ValueError, RuntimeError) as error:
                last_error = str(error)
                time.sleep(1)
        raise RuntimeError(f"等待 WebUI 就绪超时：{last_error}")
    except BaseException:
        if process is not None and process.poll() is None:
            # Popen's live child handle is authoritative even if PID-file creation failed.
            process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                say(
                    f"新启动的 WebUI 进程 {process.pid} 未能正常退出，请查看 {SERVER_LOG}。"
                )
        if state and (process is None or process.poll() is not None):
            PID_FILE.unlink(missing_ok=True)
        if receipt.is_file():
            try:
                run_step(
                    "回收本次启动的 Polaris 组件（保留之前运行的组件）",
                    [str(POLARIS), "rollback", str(receipt)],
                )
            except RuntimeError as error:
                print(error, file=sys.stderr)
        tail(SERVER_LOG)
        raise


def stop_engines() -> None:
    """Stop the local demo engines this project started; data and volumes are kept."""
    say("→ 停止本地示例引擎（保留数据）")
    subprocess.run(
        [str(CORE_PYTHON), str(ROOT / "scripts" / "local-engines.py"), "stop"],
        cwd=ROOT, env=environment(), stdin=subprocess.DEVNULL, check=False,
    )


def engines(pull: bool) -> int:
    """Start the local demo engines; a missing optional engine is a warning, not a failure."""
    child = environment()
    # The pinned images are downloaded once and reused by every later start, so a
    # plain start provisions them too: otherwise StarRocks, Doris and Hive stay
    # offline in the WebUI until someone happens to run ./start-web.sh engines.
    # LATTICE_ENGINE_PULL=0 keeps a start from downloading anything.
    child["LATTICE_ENGINE_PULL"] = os.environ.get("LATTICE_ENGINE_PULL", "1" if pull else "0")
    say("→ 启动本地示例引擎（MySQL、ClickHouse、PostgreSQL、Paimon、Iceberg、StarRocks、Doris、Hive）")
    result = subprocess.run(
        [str(CORE_PYTHON), str(ROOT / "scripts" / "local-engines.py"), "start"],
        cwd=ROOT, env=child, stdin=subprocess.DEVNULL, check=False,
    )
    return result.returncode


def status(port: int) -> int:
    state = clean_stale_state(read_state())
    if not state:
        if listener_pids(port):
            try:
                if existing_service(port):
                    return 0
            except RuntimeError as error:
                say(str(error))
                return 1
        say(f"WebUI 已停止。启动命令：./start-web.sh start（端口 {port}）")
        return 1
    try:
        data = health(state["port"], state["instance_id"])
        if listener_pids(state["port"]) != {state["pid"]}:
            raise RuntimeError("监听进程不匹配。")
        print_ready(state["port"])
        say(
            "Polaris 状态：" + str(data["services"]["polaris"].get("status", "unknown"))
        )
        return 0 if data["services"]["polaris"].get("status") == "online" else 1
    except (OSError, ValueError, RuntimeError) as error:
        say(f"WebUI 进程存在，但健康检查失败：{error}")
        return 1


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else "start"
    if len(sys.argv) > 2 or command not in {"start", "stop", "restart", "status", "engines"}:
        raise RuntimeError("用法：./start-web.sh [start|stop|restart|status|engines]")
    try:
        port = int(_env("WEB_PORT", "8787"))
    except ValueError as error:
        raise RuntimeError("LATTICE_WEB_PORT 必须是 1024–65535 之间的整数。") from error
    if not 1024 <= port <= 65535:
        raise RuntimeError("LATTICE_WEB_PORT 必须是 1024–65535 之间的整数。")
    if not shutil.which("lsof") or not shutil.which("ps"):
        raise RuntimeError("启动器需要 lsof 和 ps 来验证进程所有权，请安装后重试。")
    os.umask(0o077)
    RUNTIME.mkdir(parents=True, exist_ok=True, mode=0o700)
    RUNTIME.chmod(0o700)
    with (RUNTIME / "control.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            say("另一个 WebUI 管理任务正在运行，等待其完成…")
            fcntl.flock(lock, fcntl.LOCK_EX)
        if command == "status":
            return status(port)
        if command == "engines":
            return engines(pull=True)
        if command in {"stop", "restart"}:
            state = clean_stale_state(read_state())
            if not state and listener_pids(port):
                raise RuntimeError(
                    "端口上存在未由启动器管理的进程，拒绝停止或重启；请使用原启动方式管理。"
                )
            stop_backend(state)
            if command == "stop":
                # A restart reuses the running engines; only a full stop shuts them down.
                stop_engines()
            run_step("停止项目 Polaris 服务（保留数据）", [str(POLARIS), "stop"])
        if command in {"start", "restart"}:
            start(port)
            # Optional local demo engines: a machine without Docker still gets a
            # working WebUI, DuckDB, PostgreSQL and Polaris.
            if engines(pull=True):
                say("部分本地示例引擎未启动；WebUI 其余功能不受影响。"
                    "请按上面每个引擎的提示处理后重新运行 ./start-web.sh start。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("操作已中断。", file=sys.stderr)
        sys.exit(130)
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
        print("WebUI 操作失败：" + str(error), file=sys.stderr)
        sys.exit(1)
