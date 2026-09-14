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

"""Docker-backed local demo engines used by scripts/local-engines.py.

StarRocks, Apache Doris and Apache Hive have no native macOS builds, so they
run as pinned official container images (integrations/engines/docker.json):

  starrocks   starrocks/allin1-ubuntu      FE MySQL protocol 127.0.0.1:19030, HTTP 18030
  doris       apache/doris fe + be         FE MySQL protocol 127.0.0.1:29030, HTTP 28030
  hive        apache/hive (HiveServer2)    Thrift 127.0.0.1:20000, web UI 20002

Every container is created with the label ``lattice.project=<repository path>``;
only containers carrying that label are ever started, stopped or inspected, so
nothing that this project did not create is touched.  Ports bind loopback only,
data lives in named Docker volumes, and ``stop`` keeps the containers and
volumes so the demo data survives restarts.
"""

from __future__ import annotations

import datetime as dt
import decimal
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / ".runtime" / "engines"
PIN_FILE = ROOT / "integrations" / "engines" / "docker.json"
LABEL = "lattice.project"
CONFIG_LABEL = "lattice.config"
NETWORK = "lattice-engines"
SUBNETS = ("172.29.250.0/24", "172.30.250.0/24", "10.213.250.0/24")
DEMO_DB = "lattice_demo"
DEMO_USER = "lattice"
DOCKER_ENGINES = ("starrocks", "doris", "hive")
LABELS = {"starrocks": "StarRocks", "doris": "Apache Doris", "hive": "Apache Hive"}
PORTS = {"starrocks": 19030, "doris": 29030, "hive": 20000}
HTTP_PORTS = {"starrocks": 18030, "doris": 28030, "hive": 20002}
CONTAINERS = {
    "starrocks": ("lattice-starrocks",),
    "doris": ("lattice-doris-fe", "lattice-doris-be"),
    "hive": ("lattice-hive",),
}
MEMORY = {"lattice-starrocks": "3g", "lattice-doris-fe": "2g", "lattice-doris-be": "2500m", "lattice-hive": "2g"}
DOCKER_TIMEOUT = 600
PULL_TIMEOUT = 3600
# Docker restarts these with the daemon, so a reboot does not leave the WebUI
# showing StarRocks, Doris and Hive as offline. An explicit ./start-web.sh stop
# uses "docker stop", which Docker remembers and does not undo.
RESTART_POLICY = "unless-stopped"
OUTCOMES = {
    "reused": "复用运行中的容器",
    "restarted": "重新启动已有容器",
    "created": "新建容器",
    "recreated": "按新镜像重建容器",
}
DORIS_FE_HOST_OFFSET = 10
DORIS_BE_HOST_OFFSET = 11
READINESS_TABLE = "lattice_readiness_probe"
HIVE_METASTORE_DIR = "/opt/hive/metastore"
HIVE_DERBY_DIR = HIVE_METASTORE_DIR + "/metastore_db"
# The alphabet secrets.token_urlsafe produces; no quote or backslash can appear.
SAFE_PASSWORD = re.compile(r"[A-Za-z0-9_-]{16,128}")


class EngineUnavailable(Exception):
    """Docker or the image cannot be used on this machine; reported, not a failure."""


# --------------------------------------------------------------------------- docker helpers

def docker_binary() -> str | None:
    for candidate in (shutil.which("docker"), "/opt/homebrew/bin/docker", "/usr/local/bin/docker",
                      "/Applications/Docker.app/Contents/Resources/bin/docker"):
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    return None


def docker(*args: str, timeout: int = 120, check: bool = True, input_text: str | None = None) -> str:
    binary = docker_binary()
    if not binary:
        raise EngineUnavailable("未找到 docker 命令")
    env = {**os.environ, "DOCKER_CLI_HINTS": "false"}
    result = subprocess.run([binary, *args], text=True, capture_output=True, env=env,
                            input=input_text, stdin=None if input_text is not None else subprocess.DEVNULL,
                            timeout=timeout, check=False)
    if check and result.returncode:
        raise RuntimeError(f"docker {' '.join(args[:3])} 失败：{(result.stderr or result.stdout).strip()[:400]}")
    return result.stdout.strip()


_daemon_cache: dict[str, str | None] = {}


def docker_ready() -> str:
    """Return the Docker server version, or raise EngineUnavailable with the reason."""
    if "version" in _daemon_cache:
        if _daemon_cache["version"]:
            return _daemon_cache["version"]
        raise EngineUnavailable(_daemon_cache["error"] or "Docker 不可用")
    if not docker_binary():
        _daemon_cache.update(version=None, error="未安装 Docker（macOS 可安装 Docker Desktop 或 OrbStack）")
        raise EngineUnavailable(_daemon_cache["error"])
    try:
        version = docker("info", "--format", "{{.ServerVersion}}", timeout=30)
    except (RuntimeError, subprocess.TimeoutExpired) as error:
        _daemon_cache.update(version=None, error=f"Docker 守护进程未运行（{str(error)[:120]}）")
        raise EngineUnavailable(_daemon_cache["error"]) from error
    _daemon_cache.update(version=version, error=None)
    return version


def docker_memory() -> int:
    """Memory available to the Docker VM in bytes, or 0 when it cannot be read."""
    try:
        return int(docker("info", "--format", "{{.MemTotal}}", timeout=30))
    except (RuntimeError, ValueError, subprocess.TimeoutExpired):
        return 0


def memory_bytes(limit: str) -> int:
    """Parse a Docker memory limit such as "2500m"."""
    units = {"b": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}
    return int(float(limit[:-1]) * units[limit[-1].lower()])


GIB = 1024 ** 3
# What the engines really occupy, not what MEMORY caps them at. Measured with
# "docker stats" on the seeded demo data: StarRocks 1.3 GB, Doris FE 0.9 GB plus
# BE 1.3 GB, Hive 0.8 GB; the values below add headroom for running a query. The
# caps in MEMORY are deliberately higher and their sum is not a requirement, so
# comparing the Docker VM against that sum would warn on a machine that works.
FOOTPRINT = {"starrocks": 2 * GIB, "doris": 3 * GIB, "hive": 1.5 * GIB}


def memory_advice() -> str:
    """How to fix a Docker VM too small for the engines, or "" when it is big enough."""
    total = docker_memory()
    needed = sum(FOOTPRINT.values())
    if not total or total >= needed:
        return ""
    return (f"Docker 可用内存 {total / GIB:.1f} GB，少于 StarRocks、Doris 和 Hive 同时运行需要的约 "
            f"{needed / GIB:.1f} GB。请在 Docker Desktop → Settings → Resources → Memory 调到 "
            f"{int(needed / GIB) + 2} GB（或只启动其中一两个引擎）后重试。")


_warned: set[str] = set()


def warn_if_memory_tight() -> None:
    """Say once per run that the Docker VM may be too small, without refusing to try."""
    advice = memory_advice()
    if advice and "memory" not in _warned:
        _warned.add("memory")
        print(f"  ⚠ {advice}", flush=True)


def pins() -> dict:
    try:
        data = json.loads(PIN_FILE.read_text())
    except (OSError, ValueError) as error:
        raise RuntimeError(f"镜像固定文件无效：{PIN_FILE}：{error}") from error
    if not isinstance(data, dict) or not isinstance(data.get("images"), dict):
        raise RuntimeError(f"镜像固定文件缺少 images：{PIN_FILE}")
    return data


def image_platform() -> str:
    machine = platform.machine().lower()
    return "linux/arm64" if machine in ("arm64", "aarch64") else "linux/amd64"


def image_digest(reference: str) -> str | None:
    try:
        output = docker("image", "inspect", reference, "--format", "{{json .RepoDigests}}")
    except RuntimeError:
        return None
    digests = json.loads(output or "[]")
    for entry in digests:
        if "@sha256:" in entry:
            return entry.split("@", 1)[1]
    return None


def image_exists(reference: str) -> bool:
    try:
        docker("image", "inspect", reference, "--format", "{{.Id}}")
        return True
    except RuntimeError:
        return False


def pull_allowed() -> bool:
    """Downloading several GB of images happens only on an explicit engine command."""
    return os.environ.get("LATTICE_ENGINE_PULL", "1") != "0"


def ensure_image(key: str, log: Path) -> str:
    """Pull the pinned image when missing and verify its digest when one is pinned."""
    entry = pins()["images"].get(key)
    if not isinstance(entry, dict) or not entry.get("image"):
        raise RuntimeError(f"镜像固定文件缺少 {key}")
    reference = entry["image"]
    pinned = entry.get("digest")
    present = image_digest(reference)
    if present is None and pinned and image_exists(reference):
        raise RuntimeError(
            f"本机已有同名镜像 {reference}，但它没有仓库摘要（通常是本地构建的镜像）。"
            f"为避免覆盖该镜像，未拉取固定版本；请重命名本机镜像后重试。"
        )
    if present is None and not pull_allowed():
        raise EngineUnavailable(
            f"尚未下载镜像 {reference}（约数 GB）。需要时运行："
            f"./start-web.sh engines"
        )
    if present is None:
        target = f"{reference.split(':')[0]}@{pinned}" if pinned else reference
        pull_image(target, log)
        if pinned:
            docker("tag", target, reference)
        present = image_digest(reference)
    if pinned and present != pinned:
        raise RuntimeError(f"镜像 {reference} 的摘要 {present} 与固定值 {pinned} 不符，拒绝使用。")
    return reference


def human_duration(seconds: float) -> str:
    minutes, rest = divmod(int(seconds), 60)
    return f"{minutes} 分 {rest:02d} 秒" if minutes else f"{rest} 秒"


PULL_STATUSES = ("Pulling fs layer", "Waiting", "Downloading", "Verifying Checksum",
                 "Download complete", "Extracting", "Pull complete", "Already exists")
PULL_DONE = ("Pull complete", "Already exists")


def pull_image(target: str, log: Path) -> None:
    """Download a pinned image, reporting progress so a multi-GB pull is not a silent wait.

    ``docker pull`` writes no byte counts when its output is a pipe, so progress is
    reported as the number of layers it has finished, refreshed at most every 20
    seconds. The full output still goes to the engine log.
    """
    print(f"  下载镜像 {target}（首次启动需要数 GB，请保持网络畅通）…", flush=True)
    started = time.monotonic()
    layers: set[str] = set()
    finished: set[str] = set()
    announced = 0.0
    expired = threading.Event()
    with log.open("a") as stream:
        stream.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} docker pull {target}\n")
        stream.flush()
        process = subprocess.Popen(
            [docker_binary(), "pull", "--platform", image_platform(), target],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            text=True, bufsize=1, env={**os.environ, "DOCKER_CLI_HINTS": "false"},
        )

        def give_up():
            # Reading a pipe blocks, so the overall limit needs its own timer.
            expired.set()
            process.kill()

        watchdog = threading.Timer(PULL_TIMEOUT, give_up)
        watchdog.start()
        try:
            for line in process.stdout:
                stream.write(line)
                layer, separator, status = line.partition(": ")
                status = status.strip()
                if separator and status.split(" [")[0] in PULL_STATUSES:
                    layers.add(layer)
                    if status in PULL_DONE:
                        finished.add(layer)
                elapsed = time.monotonic() - started
                if elapsed - announced >= 20:
                    announced = elapsed
                    print(f"    …… 已完成 {len(finished)}/{len(layers)} 层，用时 {human_duration(elapsed)}",
                          flush=True)
            code = process.wait(timeout=120)
        except BaseException:
            process.kill()
            process.wait()
            raise
        finally:
            watchdog.cancel()
            process.stdout.close()
    if expired.is_set():
        raise RuntimeError(f"拉取镜像 {target} 超过 {PULL_TIMEOUT // 60} 分钟，已中止；日志：{log}")
    if code:
        stream_tail = log.read_text(errors="replace").splitlines()[-6:]
        raise RuntimeError(f"拉取镜像 {target} 失败（退出码 {code}）：{' / '.join(stream_tail)[:300]}；日志：{log}")
    print(f"  ✓ 镜像 {target} 已下载（{len(layers)} 层，用时 {human_duration(time.monotonic() - started)}）",
          flush=True)


def container(name: str) -> dict | None:
    """Inspect a container; a foreign container using our name is an error, not ours."""
    try:
        output = docker(
            "inspect", name, "--format",
            '{"status":"{{.State.Status}}","label":"{{index .Config.Labels "' + LABEL + '"}}'
            '","image":"{{.Config.Image}}","config":"{{index .Config.Labels "' + CONFIG_LABEL + '"}}"'
            ',"oomkilled":"{{.State.OOMKilled}}","exit":"{{.State.ExitCode}}"}',
        )
    except RuntimeError as error:
        if "No such object" in str(error) or "no such" in str(error).lower():
            return None
        raise
    data = json.loads(output)
    if data.get("label") != str(ROOT):
        raise RuntimeError(f"容器名 {name} 已被本项目之外的容器占用，未做任何改动。")
    return data


VOLUMES = {
    "starrocks": ("lattice-starrocks-fe-meta", "lattice-starrocks-be-storage"),
    "doris": ("lattice-doris-fe-meta", "lattice-doris-be-storage"),
    "hive": ("lattice-hive-warehouse", "lattice-hive-metastore"),
}


def volume_users(name: str) -> list[str]:
    """Names of containers, running or not, that mount this volume."""
    output = docker("ps", "-a", "--filter", f"volume={name}", "--format", "{{.Names}}", check=False)
    return [line.strip() for line in output.splitlines() if line.strip()]


def ensure_volume(name: str, owner: str | None = None, image: str | None = None) -> str:
    """Create a labelled volume, or adopt one this project demonstrably created.

    Docker cannot add a label to an existing volume, so a volume created by an
    earlier version of this script carries none. Such a volume is accepted only
    when the containers mounting it are this project's own; anything else is
    refused rather than mounted, so another program's data is never written to.

    A new volume mounted onto a path that does not exist in the image belongs to
    root, which a server running as an unprivileged user cannot write to, so
    ``owner`` (``uid:gid``) hands the fresh volume to that user once.
    """
    try:
        label = docker("volume", "inspect", name, "--format", '{{index .Labels "' + LABEL + '"}}')
    except RuntimeError as error:
        if "no such" not in str(error).lower() and "not found" not in str(error).lower():
            raise
        docker("volume", "create", "--label", f"{LABEL}={ROOT}", name)
        if owner and image:
            docker("run", "--rm", "--user", "0:0", "--label", f"{LABEL}={ROOT}",
                   "-v", f"{name}:/lattice-volume", "--entrypoint", "chown",
                   image, "-R", owner, "/lattice-volume", timeout=180)
        return name
    if label == str(ROOT):
        return name
    if label in ("", "<no value>"):
        users = volume_users(name)
        ours = [user for group in CONTAINERS.values() for user in group]
        if users and all(user in ours and container(user) is not None for user in users):
            # Created by an earlier run of this script, before volumes were labelled.
            return name
    raise RuntimeError(
        f"Docker 卷 {name} 不属于本项目，未挂载也未修改。请重命名该卷或删除后重试。"
    )


def ensure_network() -> tuple[str, str]:
    """Create the private engine network (Doris needs fixed addresses); returns (name, subnet)."""
    try:
        subnet = docker("network", "inspect", NETWORK, "--format", "{{(index .IPAM.Config 0).Subnet}}")
        label = docker("network", "inspect", NETWORK, "--format", '{{index .Labels "' + LABEL + '"}}')
        if label != str(ROOT):
            raise RuntimeError(f"Docker 网络 {NETWORK} 不属于本项目，未做改动。")
        return NETWORK, subnet
    except RuntimeError as error:
        if "not found" not in str(error).lower() and "no such" not in str(error).lower():
            raise
    last = ""
    for subnet in SUBNETS:
        try:
            docker("network", "create", "--driver", "bridge", "--subnet", subnet,
                   "--label", f"{LABEL}={ROOT}", NETWORK)
            return NETWORK, subnet
        except RuntimeError as error:
            last = str(error)
            if "overlap" not in last.lower() and "pool" not in last.lower():
                raise
    raise RuntimeError(f"无法创建 Docker 网络 {NETWORK}：{last[:200]}")


def subnet_host(subnet: str, offset: int) -> str:
    base = subnet.split("/")[0].rsplit(".", 1)[0]
    return f"{base}.{offset}"


def run_container(name: str, image: str, args: list[str], log: Path) -> str:
    """Start (or reuse) a project-owned container.

    A container left from an older pinned image or from different run options is
    replaced rather than restarted, so a changed pin actually takes effect. The
    named volumes hold the data, so recreating the container keeps the seeded
    sample tables. Returns 'reused', 'restarted', 'created' or 'recreated'.
    """
    fingerprint = hashlib.sha256(
        json.dumps([image, MEMORY[name], RESTART_POLICY, args], sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    state = container(name)
    outcome = "created"
    if state and (state["image"] != image or state.get("config") != fingerprint):
        reason = (
            f"镜像 {state['image']} 与当前固定的 {image} 不一致"
            if state["image"] != image
            else "启动参数已变化"
        )
        print(f"  容器 {name} 的{reason}，重建容器（数据卷保留）…", flush=True)
        docker("rm", "-f", name, timeout=180)
        state, outcome = None, "recreated"
    if state:
        if state["status"] == "running":
            return "reused"
        docker("start", name, timeout=120)
        return "restarted"
    command = ["run", "-d", "--name", name, "--label", f"{LABEL}={ROOT}",
               "--label", f"{CONFIG_LABEL}={fingerprint}", "--restart", RESTART_POLICY,
               "--memory", MEMORY[name], *args, image]
    with log.open("a") as stream:
        stream.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} docker {' '.join(command)}\n")
    docker(*command, timeout=300)
    return outcome


def wait_until(check, *, timeout: int, what: str, containers: tuple[str, ...]) -> None:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        for name in containers:
            state = container(name)
            if not state or state["status"] != "running":
                if state and str(state.get("oomkilled", "")).lower() == "true":
                    raise RuntimeError(
                        f"容器 {name} 因内存不足被 Docker 终止。"
                        + (memory_advice() or "请调大 Docker 可用内存后重试。")
                    )
                raise RuntimeError(f"容器 {name} 已退出；查看日志：docker logs {name}")
        try:
            if check():
                return
        except Exception as error:  # noqa: BLE001 - keep polling until the deadline
            last = f"{type(error).__name__}: {str(error)[:160]}"
        time.sleep(3)
    raise RuntimeError(f"{what} 未在 {timeout} 秒内就绪：{last}")


def container_logs(name: str, lines: int = 40) -> str:
    try:
        return docker("logs", "--tail", str(lines), name, check=False)
    except (RuntimeError, subprocess.TimeoutExpired):
        return ""


# --------------------------------------------------------------------------- shared SQL helpers

def mysql_protocol_connect(port: int, *, user: str, password: str, database: str | None = None, timeout: int = 8):
    import pymysql

    return pymysql.connect(host="127.0.0.1", port=port, user=user, password=password, database=database,
                           connect_timeout=timeout, read_timeout=120, write_timeout=60, autocommit=True,
                           charset="utf8mb4")


def olap_type(duck_type: str) -> str:
    kind = duck_type.strip().upper()
    if kind.startswith("DECIMAL("):
        return kind
    return {"INTEGER": "INT", "INT": "INT", "BIGINT": "BIGINT", "SMALLINT": "SMALLINT", "TINYINT": "TINYINT",
            "DATE": "DATE", "TIMESTAMP": "DATETIME(6)", "DATETIME": "DATETIME(6)", "BOOLEAN": "BOOLEAN",
            "DOUBLE": "DOUBLE", "FLOAT": "FLOAT", "VARCHAR": "VARCHAR(255)", "TEXT": "VARCHAR(255)"}[kind]


def hive_type(duck_type: str) -> str:
    kind = duck_type.strip().upper()
    if kind.startswith("DECIMAL("):
        return kind
    return {"INTEGER": "INT", "INT": "INT", "BIGINT": "BIGINT", "SMALLINT": "SMALLINT", "TINYINT": "TINYINT",
            "DATE": "DATE", "TIMESTAMP": "TIMESTAMP", "DATETIME": "TIMESTAMP", "BOOLEAN": "BOOLEAN",
            "DOUBLE": "DOUBLE", "FLOAT": "FLOAT", "VARCHAR": "STRING", "TEXT": "STRING"}[kind]


def olap_create_sql(name: str, columns: list[tuple[str, str]]) -> str:
    body = ", ".join(f"`{column}` {olap_type(kind)}" for column, kind in columns)
    key = columns[0][0]
    return (f"CREATE TABLE `{DEMO_DB}`.`{name}` ({body}) DUPLICATE KEY(`{key}`) "
            f"DISTRIBUTED BY HASH(`{key}`) BUCKETS 1 PROPERTIES (\"replication_num\" = \"1\")")


def olap_seed(port: int, sample: dict[str, dict], sample_rows, summary) -> dict:
    created, existing, mismatched, total = [], [], {}, 0
    connection = mysql_protocol_connect(port, user="root", password="", timeout=10)
    with connection:
        with connection.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{DEMO_DB}`")
            for name, spec in sample.items():
                cursor.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema = %s AND table_name = %s",
                               (DEMO_DB, name))
                if cursor.fetchone()[0]:
                    cursor.execute(f"SELECT count(*) FROM `{DEMO_DB}`.`{name}`")
                    count = int(cursor.fetchone()[0])
                    if count == spec["rows"]:
                        existing.append(name)
                        total += count
                        continue
                    if count:
                        mismatched[name] = (count, spec["rows"])
                        continue
                else:
                    cursor.execute(olap_create_sql(name, spec["columns"]))
                columns = ", ".join(f"`{column}`" for column, _ in spec["columns"])
                placeholders = ", ".join(["%s"] * len(spec["columns"]))
                rows = [tuple(_olap_value(value) for value in row) for row in sample_rows(spec)]
                for start in range(0, len(rows), 500):
                    cursor.executemany(f"INSERT INTO `{DEMO_DB}`.`{name}` ({columns}) VALUES ({placeholders})",
                                       rows[start:start + 500])
                created.append(name)
                total += spec["rows"]
    return summary(created, existing, mismatched, total)


def _olap_value(value):
    if isinstance(value, decimal.Decimal):
        return str(value)
    return value


def olap_probe(port: int, user: str, password: str, names: list[str], version_sql: str) -> tuple[dict[str, int], str]:
    connection = mysql_protocol_connect(port, user=user, password=password, timeout=4)
    with connection:
        with connection.cursor() as cursor:
            cursor.execute(version_sql)
            version = str(cursor.fetchone()[0])
            cursor.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = %s", (DEMO_DB,))
            present = {row[0] for row in cursor.fetchall()}
            counts = {}
            for name in names:
                if name in present:
                    cursor.execute(f"SELECT count(*) FROM `{DEMO_DB}`.`{name}`")
                    counts[name] = int(cursor.fetchone()[0])
    return counts, version


def olap_bootstrap(engine: str, port: int, grant_sql: list[str]) -> dict:
    """Create the demo database and a dedicated read/write user; keep credentials private."""
    config_path = RUNTIME / f"{engine}.json"
    existing = None
    try:
        existing = json.loads(config_path.read_text())
    except (OSError, ValueError):
        existing = None
    password = existing["password"] if isinstance(existing, dict) and existing.get("password") else secrets.token_urlsafe(18)
    if not SAFE_PASSWORD.fullmatch(password):
        # Every password this script writes comes from token_urlsafe; anything else
        # must not be spliced into a CREATE USER statement.
        raise RuntimeError(f"{RUNTIME / (engine + '.json')} 中的密码包含不支持的字符，请删除该文件后重试。")
    connection = mysql_protocol_connect(port, user="root", password="", timeout=10)
    with connection:
        with connection.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{DEMO_DB}`")
            cursor.execute("SELECT count(*) FROM information_schema.schemata WHERE schema_name = %s", (DEMO_DB,))
            cursor.execute(f"CREATE USER IF NOT EXISTS '{DEMO_USER}'@'%' IDENTIFIED BY '{password}'")
            cursor.execute(f"ALTER USER '{DEMO_USER}'@'%' IDENTIFIED BY '{password}'")
            for statement in grant_sql:
                cursor.execute(statement)
    config = {"type": engine, "host": "127.0.0.1", "port": port, "user": DEMO_USER, "password": password,
              "database": DEMO_DB}
    if engine == "starrocks":
        config["catalog"] = "default_catalog"
    return config


# --------------------------------------------------------------------------- StarRocks

def start_starrocks(log: Path, write_config) -> str:
    docker_ready()
    warn_if_memory_tight()
    image = ensure_image("starrocks", log)
    name = CONTAINERS["starrocks"][0]
    for volume in VOLUMES["starrocks"]:
        ensure_volume(volume)
    outcome = run_container(name, image, [
        "-p", f"127.0.0.1:{PORTS['starrocks']}:9030", "-p", f"127.0.0.1:{HTTP_PORTS['starrocks']}:8030",
        "-v", "lattice-starrocks-fe-meta:/data/deploy/starrocks/fe/meta",
        "-v", "lattice-starrocks-be-storage:/data/deploy/starrocks/be/storage",
    ], log)

    def alive():
        connection = mysql_protocol_connect(PORTS["starrocks"], user="root", password="", timeout=4)
        with connection:
            with connection.cursor() as cursor:
                cursor.execute("SHOW BACKENDS")
                rows = cursor.fetchall()
                columns = [column[0] for column in cursor.description]
                index = columns.index("Alive")
                return any(str(row[index]).lower() == "true" for row in rows)

    wait_until(alive, timeout=DOCKER_TIMEOUT, what="StarRocks FE/BE", containers=(name,))
    config = olap_bootstrap("starrocks", PORTS["starrocks"], [
        f"GRANT SELECT, INSERT ON ALL TABLES IN DATABASE `{DEMO_DB}` TO USER '{DEMO_USER}'@'%'",
        f"GRANT CREATE TABLE ON DATABASE `{DEMO_DB}` TO USER '{DEMO_USER}'@'%'",
    ])
    write_config("starrocks", config)
    _, version = olap_probe(PORTS["starrocks"], DEMO_USER, config["password"], [], "SELECT current_version()")
    note = OUTCOMES[outcome]
    return f"StarRocks {version} 已就绪：127.0.0.1:{PORTS['starrocks']}（MySQL 协议，用户 {DEMO_USER}，{note}）"


def seed_starrocks(sample, sample_rows, summary) -> dict:
    return olap_seed(PORTS["starrocks"], sample, sample_rows, summary)


# --------------------------------------------------------------------------- Apache Doris

def doris_fe_conf_dir(image: str) -> Path:
    """Materialise the image's FE configuration once, with a heap that fits the container."""
    conf_dir = RUNTIME / "doris" / "fe-conf"
    marker = conf_dir / ".lattice-image"
    identity = f"{image}@{image_digest(image) or 'unknown'}"
    if marker.is_file() and marker.read_text().strip() == identity and (conf_dir / "fe.conf").is_file():
        return conf_dir
    if conf_dir.exists():
        # A newer image may drop files; start from the image's own configuration.
        shutil.rmtree(conf_dir)
    conf_dir.mkdir(parents=True, exist_ok=True)
    scratch = f"lattice-doris-conf-{secrets.token_hex(4)}"
    docker("create", "--name", scratch, "--label", f"{LABEL}={ROOT}", image)
    try:
        with tempfile.TemporaryDirectory(dir=RUNTIME / "doris") as temporary:
            docker("cp", f"{scratch}:/opt/apache-doris/fe/conf/.", temporary, timeout=120)
            for item in Path(temporary).iterdir():
                target = conf_dir / item.name
                if item.is_dir():
                    shutil.copytree(item, target, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, target)
    finally:
        docker("rm", "-f", scratch, check=False)
    fe_conf = conf_dir / "fe.conf"
    lines = []
    for line in fe_conf.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("JAVA_OPTS") and "-Xmx" in stripped:
            key, value = stripped.split("=", 1)
            value = value.strip().strip('"')
            parts = [part for part in value.split() if not part.startswith("-Xmx") and not part.startswith("-Xms")]
            parts.insert(1, "-Xmx1024m")
            line = f'{key.strip()}="{" ".join(parts)}"'
        lines.append(line)
    if not any(line.startswith("sys_log_verbose_modules") for line in lines):
        lines.append("")
    lines.append("# Lattice: fixed by scripts/lattice_docker_engines.py")
    fe_conf.write_text("\n".join(lines) + "\n")
    os.chmod(conf_dir, 0o755)
    for item in conf_dir.iterdir():
        os.chmod(item, 0o755 if item.is_dir() else 0o644)
    marker.write_text(identity + "\n")
    return conf_dir


def start_doris(log: Path, write_config) -> str:
    docker_ready()
    warn_if_memory_tight()
    fe_image = ensure_image("doris-fe", log)
    be_image = ensure_image("doris-be", log)
    network, subnet = ensure_network()
    fe_ip = subnet_host(subnet, DORIS_FE_HOST_OFFSET)
    be_ip = subnet_host(subnet, DORIS_BE_HOST_OFFSET)
    fe_name, be_name = CONTAINERS["doris"]
    for volume in VOLUMES["doris"]:
        ensure_volume(volume)
    conf_dir = doris_fe_conf_dir(fe_image)
    fe_outcome = run_container(fe_name, fe_image, [
        "--network", network, "--ip", fe_ip,
        "-p", f"127.0.0.1:{PORTS['doris']}:9030", "-p", f"127.0.0.1:{HTTP_PORTS['doris']}:8030",
        "-e", f"FE_SERVERS=fe1:{fe_ip}:9010", "-e", "FE_ID=1",
        "-v", "lattice-doris-fe-meta:/opt/apache-doris/fe/doris-meta",
        "-v", f"{conf_dir}:/opt/apache-doris/fe/conf",
    ], log)

    def fe_alive():
        connection = mysql_protocol_connect(PORTS["doris"], user="root", password="", timeout=4)
        with connection:
            with connection.cursor() as cursor:
                cursor.execute("SHOW FRONTENDS")
                rows = cursor.fetchall()
                columns = [column[0] for column in cursor.description]
                index = columns.index("Alive")
                return any(str(row[index]).lower() == "true" for row in rows)

    wait_until(fe_alive, timeout=DOCKER_TIMEOUT, what="Doris FE", containers=(fe_name,))
    be_outcome = run_container(be_name, be_image, [
        "--network", network, "--ip", be_ip,
        "-e", f"FE_SERVERS=fe1:{fe_ip}:9010", "-e", f"BE_ADDR={be_ip}:9050", "-e", "SKIP_CHECK_ULIMIT=true",
        "-v", "lattice-doris-be-storage:/opt/apache-doris/be/storage",
    ], log)

    def be_alive():
        connection = mysql_protocol_connect(PORTS["doris"], user="root", password="", timeout=4)
        with connection:
            with connection.cursor() as cursor:
                cursor.execute("SHOW BACKENDS")
                rows = cursor.fetchall()
                columns = [column[0] for column in cursor.description]
                index = columns.index("Alive")
                return any(str(row[index]).lower() == "true" for row in rows)

    wait_until(be_alive, timeout=DOCKER_TIMEOUT, what="Doris BE", containers=(fe_name, be_name))

    def scan_works():
        """A registered backend is not yet a usable one; run a real scan to be sure."""
        connection = mysql_protocol_connect(PORTS["doris"], user="root", password="", timeout=6)
        with connection:
            with connection.cursor() as cursor:
                cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{DEMO_DB}`")
                cursor.execute(
                    f"CREATE TABLE IF NOT EXISTS `{DEMO_DB}`.`{READINESS_TABLE}` (id INT) "
                    "DUPLICATE KEY(id) DISTRIBUTED BY HASH(id) BUCKETS 1 "
                    'PROPERTIES ("replication_num" = "1")'
                )
                cursor.execute(f"INSERT INTO `{DEMO_DB}`.`{READINESS_TABLE}` VALUES (1)")
                cursor.execute(f"SELECT count(*) FROM `{DEMO_DB}`.`{READINESS_TABLE}`")
                if int(cursor.fetchone()[0]) < 1:
                    return False
                cursor.execute(f"DROP TABLE `{DEMO_DB}`.`{READINESS_TABLE}`")
                return True

    wait_until(scan_works, timeout=DOCKER_TIMEOUT, what="Doris 查询链路", containers=(fe_name, be_name))
    config = olap_bootstrap("doris", PORTS["doris"], [
        f"GRANT SELECT_PRIV, LOAD_PRIV, CREATE_PRIV, DROP_PRIV, ALTER_PRIV ON `{DEMO_DB}`.* TO '{DEMO_USER}'@'%'",
    ])
    write_config("doris", config)
    _, version = olap_probe(PORTS["doris"], DEMO_USER, config["password"], [], "SELECT @@version_comment")
    short = {"reused": "复用", "restarted": "重启", "created": "新建", "recreated": "重建"}
    return (f"{version} 已就绪：127.0.0.1:{PORTS['doris']}（MySQL 协议，用户 {DEMO_USER}，"
            f"FE {short[fe_outcome]} / BE {short[be_outcome]}）")


def seed_doris(sample, sample_rows, summary) -> dict:
    return olap_seed(PORTS["doris"], sample, sample_rows, summary)


# --------------------------------------------------------------------------- Apache Hive

def hive_connect(database: str = "default", timeout: int = 8):
    from pyhive import hive

    return hive.connect(host="127.0.0.1", port=PORTS["hive"], username="hive", database=database, auth="NONE")


def start_hive(log: Path, write_config) -> str:
    docker_ready()
    warn_if_memory_tight()
    image = ensure_image("hive", log)
    name = CONTAINERS["hive"][0]
    # The Hive image runs as uid 1000; a fresh volume would otherwise be root-owned.
    for volume in VOLUMES["hive"]:
        ensure_volume(volume, owner="1000:1000", image=image)
    # Three restart hazards are handled here:
    #  * HiveServer2 refuses to start while its pid file from the previous run
    #    exists, so the pid directory is a tmpfs that is empty on every start;
    #  * Derby will not create its database in a directory that already exists, so
    #    the volume is mounted one level up and the database is a subdirectory that
    #    Derby creates itself (the documented SERVICE_OPTS override);
    #  * the warehouse and the metastore live in named volumes, so the demo data
    #    survives both a restart and a container rebuild.
    outcome = run_container(name, image, [
        "-p", f"127.0.0.1:{PORTS['hive']}:10000", "-p", f"127.0.0.1:{HTTP_PORTS['hive']}:10002",
        "-e", "SERVICE_NAME=hiveserver2",
        "-e", "HIVESERVER2_PID_DIR=/opt/hive/pids",
        "-e", f"SERVICE_OPTS=-Djavax.jdo.option.ConnectionURL=jdbc:derby:;databaseName={HIVE_DERBY_DIR};create=true",
        "--tmpfs", "/opt/hive/pids:mode=1777",
        "-v", "lattice-hive-warehouse:/opt/hive/data/warehouse",
        f"-v", f"lattice-hive-metastore:{HIVE_METASTORE_DIR}",
    ], log)

    def alive():
        connection = hive_connect()
        try:
            cursor = connection.cursor()
            cursor.execute("SELECT version()")
            return bool(cursor.fetchone())
        finally:
            connection.close()

    wait_until(alive, timeout=DOCKER_TIMEOUT, what="HiveServer2", containers=(name,))
    config = {"type": "hive", "host": "127.0.0.1", "port": PORTS["hive"], "user": "hive", "password": "",
              "database": DEMO_DB, "auth": "NONE"}
    write_config("hive", config)
    connection = hive_connect()
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT version()")
        version = str(cursor.fetchone()[0]).split(" ")[0]
    finally:
        connection.close()
    note = OUTCOMES[outcome]
    return f"Hive {version} 已就绪：127.0.0.1:{PORTS['hive']}（HiveServer2，用户 hive，{note}）"


def _hive_text(value) -> str:
    if value is None:
        return "\\N"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dt.datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S.%f")
    if isinstance(value, (dt.date,)):
        return value.isoformat()
    text = str(value)
    return text.replace("\\", "\\\\").replace("\x01", " ").replace("\n", " ").replace("\r", " ")


def seed_hive(sample, sample_rows, summary) -> dict:
    name = CONTAINERS["hive"][0]
    if (container(name) or {}).get("status") != "running":
        raise EngineUnavailable("Hive 容器未在运行")
    created, existing, mismatched, total = [], [], {}, 0
    connection = hive_connect()
    try:
        cursor = connection.cursor()
        cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{DEMO_DB}`")
        cursor.execute(f"SHOW TABLES IN `{DEMO_DB}`")
        present = {row[0] for row in cursor.fetchall()}
        docker("exec", name, "mkdir", "-p", "/tmp/lattice-import")
        for table, spec in sample.items():
            if table in present:
                cursor.execute(f"SELECT count(*) FROM `{DEMO_DB}`.`{table}`")
                count = int(cursor.fetchone()[0])
                if count == spec["rows"]:
                    existing.append(table)
                    total += count
                    continue
                if count:
                    mismatched[table] = (count, spec["rows"])
                    continue
            else:
                body = ", ".join(f"`{column}` {hive_type(kind)}" for column, kind in spec["columns"])
                cursor.execute(
                    f"CREATE TABLE `{DEMO_DB}`.`{table}` ({body}) ROW FORMAT DELIMITED FIELDS TERMINATED BY '\\001' "
                    "NULL DEFINED AS '\\\\N' STORED AS TEXTFILE"
                )
            with tempfile.NamedTemporaryFile("w", suffix=".tsv", delete=False, encoding="utf-8") as stream:
                for row in sample_rows(spec):
                    stream.write("\x01".join(_hive_text(value) for value in row) + "\n")
                temporary = Path(stream.name)
            try:
                os.chmod(temporary, 0o644)
                docker("cp", str(temporary), f"{name}:/tmp/lattice-import/{table}.tsv", timeout=120)
            finally:
                temporary.unlink(missing_ok=True)
            cursor.execute(f"LOAD DATA LOCAL INPATH '/tmp/lattice-import/{table}.tsv' OVERWRITE INTO TABLE `{DEMO_DB}`.`{table}`")
            cursor.execute(f"ANALYZE TABLE `{DEMO_DB}`.`{table}` COMPUTE STATISTICS")
            created.append(table)
            total += spec["rows"]
        docker("exec", name, "rm", "-rf", "/tmp/lattice-import", check=False)
    finally:
        connection.close()
    return summary(created, existing, mismatched, total)


def probe_hive(names: list[str]) -> tuple[dict[str, int], str]:
    connection = hive_connect(timeout=4)
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT version()")
        version = "Hive " + str(cursor.fetchone()[0]).split(" ")[0]
        cursor.execute("SHOW DATABASES")
        if DEMO_DB not in {row[0] for row in cursor.fetchall()}:
            return {}, version
        cursor.execute(f"SHOW TABLES IN `{DEMO_DB}`")
        present = {row[0] for row in cursor.fetchall()}
        counts = {}
        for name in names:
            if name in present:
                cursor.execute(f"SELECT count(*) FROM `{DEMO_DB}`.`{name}`")
                counts[name] = int(cursor.fetchone()[0])
        return counts, version
    finally:
        connection.close()


# --------------------------------------------------------------------------- registry

STARTERS = {"starrocks": start_starrocks, "doris": start_doris, "hive": start_hive}
SEEDERS = {"starrocks": seed_starrocks, "doris": seed_doris, "hive": seed_hive}


def probe(engine: str, names: list[str], config: dict | None) -> tuple[dict[str, int], str]:
    if not config:
        raise EngineUnavailable("尚未初始化")
    if engine == "hive":
        return probe_hive(names)
    version_sql = "SELECT current_version()" if engine == "starrocks" else "SELECT @@version_comment"
    return olap_probe(PORTS[engine], config["user"], config["password"], names, version_sql)


def installed(engine: str) -> tuple[bool, str]:
    try:
        version = docker_ready()
    except EngineUnavailable as error:
        return False, str(error)
    try:
        entries = pins()["images"]
    except RuntimeError as error:
        return False, str(error)
    keys = {"starrocks": ["starrocks"], "doris": ["doris-fe", "doris-be"], "hive": ["hive"]}[engine]
    missing = [entries[key]["image"] for key in keys if image_digest(entries[key]["image"]) is None]
    if missing:
        return True, f"Docker {version}；镜像 {', '.join(missing)} 尚未拉取，首次启动时下载"
    return True, f"Docker {version} · " + ", ".join(entries[key]["image"] for key in keys)


def running(engine: str) -> bool:
    try:
        return all((container(name) or {}).get("status") == "running" for name in CONTAINERS[engine])
    except (RuntimeError, EngineUnavailable, subprocess.TimeoutExpired):
        return False


def stop(engine: str, say) -> None:
    try:
        docker_ready()
    except EngineUnavailable as error:
        say(f"  {LABELS[engine]}：{error}")
        return
    for name in reversed(CONTAINERS[engine]):
        try:
            state = container(name)
        except RuntimeError as error:
            say(f"  ⚠ {error}")
            continue
        if state and state["status"] == "running":
            say(f"→ 停止本地 {LABELS[engine]} 容器 {name}")
            docker("stop", "-t", "60", name, timeout=120)
            say(f"  ✓ {name} 已停止，数据卷保留")
        elif state:
            say(f"  {name} 未运行")
        else:
            say(f"  本地 {LABELS[engine]} 容器 {name} 不存在")
