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

"""Start, seed and stop the local demo engines behind the WebUI datasource page.

Engines:
  mysql       Homebrew mysqld, project data directory, 127.0.0.1:33306
  clickhouse  pinned official binary (integrations/engines/clickhouse.json), 127.0.0.1:18123 / 19009
  postgres    database lattice_demo inside the project PostgreSQL cluster started by Polaris (127.0.0.1:55432)
  paimon      Apache Paimon filesystem warehouse under .runtime/engines/paimon/warehouse (no process)
  iceberg     demo tables written into the local Apache Polaris catalog (no process)

Every process binds loopback only, is project-owned and is tracked with the same
pid + ``ps`` identity record as integrations/polaris/service.py: nothing that this
script did not start is ever signalled.  Seeding only creates missing tables.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lattice_docker_engines as docker_engines  # noqa: E402 (project module, path set above)

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / ".runtime" / "engines"
DOWNLOADS = ROOT / ".runtime" / "downloads"
POLARIS_RUNTIME = ROOT / ".runtime" / "polaris"
WEBUI_RUNTIME = ROOT / ".runtime" / "webui"
PIN_FILE = ROOT / "integrations" / "engines" / "clickhouse.json"
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))

MYSQL_PORT = 33306
CLICKHOUSE_HTTP_PORT = 18123
CLICKHOUSE_TCP_PORT = 19009
PG_PORT = 55432
POLARIS_PORT = 8181
POLARIS_HEALTH_PORT = 8182
DEMO_DB = "lattice_demo"
DEMO_USER = "lattice"
PG_DEMO_USER = "lattice_demo"
PG_SUPERUSER = "lattice_polaris"
ICEBERG_CATALOG = "lattice"
ICEBERG_NAMESPACE = ("demo",)
ENGINES = ("mysql", "clickhouse", "postgres", "paimon", "iceberg", "starrocks", "doris", "hive")
LABELS = {
    "mysql": "MySQL",
    "clickhouse": "ClickHouse",
    "postgres": "PostgreSQL 示例库",
    "paimon": "Paimon",
    "iceberg": "Iceberg",
    "starrocks": "StarRocks",
    "doris": "Apache Doris",
    "hive": "Apache Hive",
}
PORTS = {
    "mysql": MYSQL_PORT,
    "clickhouse": CLICKHOUSE_HTTP_PORT,
    "postgres": PG_PORT,
    "paimon": None,
    "iceberg": POLARIS_PORT,
    "starrocks": docker_engines.PORTS["starrocks"],
    "doris": docker_engines.PORTS["doris"],
    "hive": docker_engines.PORTS["hive"],
}
MYSQL_CANDIDATES = (
    "/opt/homebrew/bin/mysqld",
    "/opt/homebrew/opt/mysql/bin/mysqld",
    "/usr/local/bin/mysqld",
    "/usr/local/opt/mysql/bin/mysqld",
    "/usr/local/mysql/bin/mysqld",
    "/usr/sbin/mysqld",
)
# PyMySQL has no ``cryptography`` module in the core environment, so the first
# caching_sha2_password authentication of an account after a server start must
# use a secure transport (TLS or the Unix socket).  A non-empty ``ssl`` mapping
# enables TLS without certificate verification (the server certificate is the
# self-signed one mysqld generates in its own data directory).
MYSQL_TLS = {"verify_mode": False}


EngineUnavailable = docker_engines.EngineUnavailable


# --------------------------------------------------------------------------- helpers

def say(message: str) -> None:
    print(message, flush=True)


def write_private(path: Path, content: str) -> None:
    """Replace a private file atomically without following an existing symlink."""
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(content)
    os.replace(temporary, path)


def write_json(path: Path, payload: dict) -> None:
    write_private(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def read_json(path: Path) -> dict | None:
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run(command: list[str], *, log: Path | None = None, env: dict | None = None,
        cwd: Path | None = None, timeout: int | None = None) -> str:
    if log:
        with log.open("a") as stream:
            stream.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(command)}\n")
            stream.flush()
            result = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, env=env,
                                    cwd=cwd, stdin=subprocess.DEVNULL, timeout=timeout, check=False)
        if result.returncode:
            raise RuntimeError(f"命令失败（退出码 {result.returncode}），日志：{log}")
        return ""
    result = subprocess.run(command, text=True, capture_output=True, env=env, cwd=cwd,
                            stdin=subprocess.DEVNULL, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError(f"命令失败（退出码 {result.returncode}）：{' '.join(command)}\n{result.stdout}{result.stderr}")
    return (result.stdout + result.stderr).strip()


def process_identity(pid: int) -> str:
    """Identity of a live process, or "" when it is gone.

    ``ps`` exits 1 when the PID does not exist; any other failure means the check
    itself could not run, and is raised so a live process is never mistaken for a
    finished one.
    """
    if pid <= 1:
        return ""
    try:
        result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart=", "-o", "command="],
                                text=True, capture_output=True, check=False, timeout=30)
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"无法通过 ps 检查进程 {pid}：{error}") from error
    if result.returncode == 0:
        return result.stdout.strip()
    if result.returncode == 1 and not result.stdout.strip():
        return ""
    raise RuntimeError(f"ps 检查进程 {pid} 失败（退出码 {result.returncode}）：{result.stderr.strip()[:200]}")


def launch_process(command: list[str], *, log_path: Path, pid_file: Path,
                   env: dict | None = None, cwd: Path | None = None) -> subprocess.Popen:
    """Keep the child handle until its ownership record has been committed."""
    process = None
    try:
        with log_path.open("a") as log:
            log.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(command)}\n")
            log.flush()
            process = subprocess.Popen(command, cwd=cwd or RUNTIME, env=env, stdin=subprocess.DEVNULL,
                                       stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                                       close_fds=True)
        identity = process_identity(process.pid)
        if not identity or process.poll() is not None:
            raise RuntimeError(f"进程在初始化前退出，日志：{log_path}")
        write_private(pid_file, json.dumps({"pid": process.pid, "identity": identity, "root": str(ROOT)}))
        return process
    except BaseException:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        raise


def split_identity(identity: str) -> tuple[str, str]:
    """Split a ``ps -o lstart= -o command=`` line into its start time and command."""
    parts = identity.split(maxsplit=5)
    if len(parts) < 6:
        return identity.strip(), ""
    return " ".join(parts[:5]), parts[5].strip()


def child_carries_marker(pid: int, marker: str) -> bool:
    """True when a child of ``pid`` still runs with our project-specific marker."""
    try:
        result = subprocess.run(["ps", "-A", "-o", "ppid=", "-o", "command="],
                                text=True, capture_output=True, check=False, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return False
    for line in result.stdout.splitlines():
        parent, _, command = line.strip().partition(" ")
        if parent.isdigit() and int(parent) == pid and marker in command:
            return True
    return False


def owned_process(pid_file: Path, marker: str, aliases: tuple[str, ...] = ()) -> dict | None:
    """Return the ownership record only if the live process still matches it.

    The process start time recorded at launch must always match, so a recycled
    PID is never mistaken for ours.  ``aliases`` lists command lines a server is
    known to rewrite itself to after launch (ClickHouse renames its supervisor
    process to ``clickhouse-watchdog``).  Such a name carries nothing specific to
    this project, so an alias match additionally requires a child process that
    still shows our marker; without the aliases a reused instance would look
    foreign and its port would be reported as taken by someone else.
    """
    if not pid_file.exists():
        return None
    try:
        state = json.loads(pid_file.read_text())
        pid = state["pid"]
        if type(pid) is not int or not isinstance(state.get("identity"), str):
            return None
        if marker not in state["identity"]:
            return None
        started, command = split_identity(state["identity"])
        live_started, live_command = split_identity(process_identity(pid))
        if not live_started or live_started != started:
            return None
        if live_command == command:
            return state
        if live_command in aliases and child_carries_marker(pid, marker):
            return state
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def stop_process(pid_file: Path, marker: str, *, timeout: int = 90,
                 aliases: tuple[str, ...] = ()) -> bool:
    """SIGTERM a project-owned process; a stale or foreign record is cleared, never killed.

    A record is only discarded once ``ps`` actually reports the process as gone or
    as someone else's; if the check cannot run, the record is kept so a live engine
    never becomes unmanageable.
    """
    state = owned_process(pid_file, marker, aliases)
    if not state:
        pid_file.unlink(missing_ok=True)
        return False
    os.kill(state["pid"], signal.SIGTERM)
    for _ in range(timeout * 5):
        if not owned_process(pid_file, marker, aliases):
            break
        time.sleep(0.2)
    else:
        raise RuntimeError(f"进程 {state['pid']} 未在 {timeout} 秒内停止；拒绝强制终止，请检查日志。")
    pid_file.unlink(missing_ok=True)
    return True


def require_free_port(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as error:
            raise RuntimeError(f"端口 {port} 已被其他进程占用，未启动或终止任何服务。") from error


def tcp_open(port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def engine_dir(name: str) -> Path:
    return RUNTIME / name


def config_file(name: str) -> Path:
    return RUNTIME / f"{name}.json"


def pid_file(name: str) -> Path:
    return RUNTIME / f"{name}.pid.json"


def read_config(name: str) -> dict | None:
    return read_json(config_file(name))


# --------------------------------------------------------------------------- sample data

def sample_table_names() -> list[str]:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    try:
        from webapi.query import TABLES  # noqa: WPS433 (project module)
    except ImportError as error:
        raise RuntimeError(f"无法导入 webapi.query（示例数据来源）：{error}") from error
    return list(TABLES)


def sample_connection():
    import duckdb

    database = WEBUI_RUNTIME / "sample.duckdb"
    if not database.is_file():
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from webapi.query import QueryStore

        QueryStore(WEBUI_RUNTIME)
    last_error: Exception | None = None
    for _ in range(20):
        try:
            return duckdb.connect(str(database), read_only=True, config={
                "enable_external_access": "false", "allow_unsigned_extensions": "false",
                "memory_limit": "256MB", "threads": "2",
            })
        except duckdb.Error as error:  # another process holds a write lock briefly
            last_error = error
            time.sleep(0.5)
    raise RuntimeError(f"无法读取示例 DuckDB {database}：{last_error}")


def normalize_arrow(table):
    import pyarrow as pa

    fields = []
    for field in table.schema:
        if pa.types.is_large_string(field.type):
            field = field.with_type(pa.string())
        elif pa.types.is_large_binary(field.type):
            field = field.with_type(pa.binary())
        fields.append(field)
    return table.cast(pa.schema(fields))


def load_sample() -> dict[str, dict]:
    """Read the six sample tables (types via DESCRIBE, rows as Arrow) from the WebUI DuckDB file."""
    names = sample_table_names()
    connection = sample_connection()
    try:
        result = {}
        for name in names:
            described = connection.execute(f'DESCRIBE "{name}"').fetchall()
            columns = [(row[0], row[1]) for row in described]
            cursor = connection.execute(f'SELECT * FROM "{name}"')
            fetch = getattr(cursor, "to_arrow_table", None) or cursor.fetch_arrow_table
            arrow = normalize_arrow(fetch())
            result[name] = {"columns": columns, "arrow": arrow, "rows": arrow.num_rows}
        return result
    finally:
        connection.close()


def expected_counts() -> dict[str, int] | None:
    if not (WEBUI_RUNTIME / "sample.duckdb").is_file():
        return None
    connection = sample_connection()
    try:
        return {name: connection.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
                for name in sample_table_names()}
    finally:
        connection.close()


def sql_type(engine: str, duck_type: str) -> str:
    """Map a DuckDB DESCRIBE type to the engine DDL type."""
    kind = duck_type.strip().upper()
    decimal_match = re.fullmatch(r"DECIMAL\((\d+)\s*,\s*(\d+)\)", kind)
    if decimal_match:
        precision, scale = decimal_match.groups()
        return {"mysql": f"DECIMAL({precision},{scale})", "postgres": f"NUMERIC({precision},{scale})",
                "clickhouse": f"Decimal({precision}, {scale})"}[engine]
    if kind in ("INTEGER", "INT", "INT4", "SIGNED"):
        return {"mysql": "INT", "postgres": "INTEGER", "clickhouse": "Int32"}[engine]
    if kind in ("BIGINT", "INT8", "HUGEINT"):
        return {"mysql": "BIGINT", "postgres": "BIGINT", "clickhouse": "Int64"}[engine]
    if kind in ("SMALLINT", "INT2", "TINYINT", "INT1"):
        return {"mysql": "SMALLINT", "postgres": "SMALLINT", "clickhouse": "Int16"}[engine]
    if kind == "DATE":
        return {"mysql": "DATE", "postgres": "DATE", "clickhouse": "Date"}[engine]
    if kind in ("TIMESTAMP", "DATETIME"):
        return {"mysql": "DATETIME(6)", "postgres": "TIMESTAMP", "clickhouse": "DateTime64(6)"}[engine]
    if kind == "BOOLEAN":
        return {"mysql": "TINYINT(1)", "postgres": "BOOLEAN", "clickhouse": "Bool"}[engine]
    if kind in ("DOUBLE", "FLOAT8"):
        return {"mysql": "DOUBLE", "postgres": "DOUBLE PRECISION", "clickhouse": "Float64"}[engine]
    if kind in ("FLOAT", "REAL", "FLOAT4"):
        return {"mysql": "FLOAT", "postgres": "REAL", "clickhouse": "Float32"}[engine]
    if kind in ("VARCHAR", "TEXT", "STRING") or kind.startswith("VARCHAR("):
        return {"mysql": "VARCHAR(255)", "postgres": "TEXT", "clickhouse": "String"}[engine]
    raise ValueError(f"不支持的示例列类型：{duck_type}")


def create_table_sql(engine: str, name: str, columns: list[tuple[str, str]]) -> str:
    if engine == "mysql":
        body = ", ".join(f"`{column}` {sql_type('mysql', kind)}" for column, kind in columns)
        return (f"CREATE TABLE `{DEMO_DB}`.`{name}` ({body}) "
                "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci")
    if engine == "postgres":
        body = ", ".join(f'"{column}" {sql_type("postgres", kind)}' for column, kind in columns)
        return f'CREATE TABLE public."{name}" ({body})'
    if engine == "clickhouse":
        body = ", ".join(f"`{column}` {sql_type('clickhouse', kind)}" for column, kind in columns)
        return (f"CREATE TABLE `{DEMO_DB}`.`{name}` ({body}) "
                f"ENGINE = MergeTree ORDER BY `{columns[0][0]}`")
    raise ValueError(engine)


def sample_rows(spec: dict) -> list[tuple]:
    names = [column for column, _ in spec["columns"]]
    return [tuple(record[column] for column in names) for record in spec["arrow"].to_pylist()]


# --------------------------------------------------------------------------- MySQL

def mysqld_path() -> Path | None:
    configured = os.environ.get("LATTICE_MYSQLD")
    candidates = [configured] + list(MYSQL_CANDIDATES) + [shutil.which("mysqld")]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return Path(candidate)
    return None


def mysql_basedir(mysqld: Path) -> Path:
    for base in (Path("/opt/homebrew/opt/mysql"), Path("/usr/local/opt/mysql")):
        try:
            if (base / "bin" / "mysqld").resolve() == mysqld.resolve():
                return base
        except OSError:
            continue
    return mysqld.resolve().parent.parent


def mysql_marker() -> str:
    return f"--datadir={engine_dir('mysql') / 'data'}"


def mysqld_command(mysqld: Path) -> list[str]:
    base = engine_dir("mysql")
    return [
        str(mysqld), "--no-defaults",
        f"--datadir={base / 'data'}", f"--basedir={mysql_basedir(mysqld)}",
        f"--port={MYSQL_PORT}", "--bind-address=127.0.0.1",
        f"--socket={base / 'mysql.sock'}", f"--pid-file={base / 'mysqld.pid'}",
        f"--log-error={base / 'mysqld.log'}", f"--tmpdir={base / 'tmp'}",
        "--mysqlx=OFF", "--skip-log-bin", "--skip-name-resolve",
        "--innodb-buffer-pool-size=128M", "--innodb-redo-log-capacity=64M",
        "--performance-schema=OFF", "--max-connections=64",
        "--character-set-server=utf8mb4", "--collation-server=utf8mb4_0900_ai_ci",
        "--local-infile=OFF", "--secure-file-priv=NULL", "--log-error-verbosity=2",
    ]


def mysql_initialize(mysqld: Path) -> bool:
    base = engine_dir("mysql")
    data = base / "data"
    if (data / "mysql").is_dir() or (data / "ibdata1").exists():
        return False
    if data.exists() and any(data.iterdir()):
        raise RuntimeError(f"MySQL 数据目录 {data} 非空但未初始化；请手动检查（脚本不会删除任何数据）。")
    say("  初始化 MySQL 数据目录（约 190 MB）…")
    run([str(mysqld), "--no-defaults", "--initialize-insecure", f"--datadir={data}",
         f"--basedir={mysql_basedir(mysqld)}", f"--log-error={base / 'mysqld.log'}",
         "--innodb-redo-log-capacity=64M", "--character-set-server=utf8mb4",
         "--collation-server=utf8mb4_0900_ai_ci"], log=base / "initialize.log", timeout=600)
    return True


def mysql_connect(*, user: str, password: str, database: str | None = None,
                  unix_socket: Path | None = None, plain: bool = False, timeout: int = 5):
    import pymysql

    options = {"user": user, "password": password, "database": database, "charset": "utf8mb4",
               "connect_timeout": timeout, "read_timeout": 60, "write_timeout": 60, "autocommit": False}
    if unix_socket:
        options["unix_socket"] = str(unix_socket)
    else:
        options.update({"host": "127.0.0.1", "port": MYSQL_PORT})
        if plain:
            options["ssl_disabled"] = True
        else:
            options["ssl"] = dict(MYSQL_TLS)
    return pymysql.connect(**options)


def mysql_bootstrap() -> dict:
    """Set the root password, create the demo account and database once; idempotent afterwards."""
    config = read_config("mysql")
    root_record = read_json(RUNTIME / "mysql-root.json") or {}
    if config and root_record.get("bootstrapped"):
        return config
    import pymysql

    base = engine_dir("mysql")
    root_password = root_record.get("root_password") or secrets.token_urlsafe(24)
    password = (config or {}).get("password") or secrets.token_urlsafe(24)
    write_json(RUNTIME / "mysql-root.json", {
        "user": "root", "root_password": root_password, "socket": str(base / "mysql.sock"),
        "port": MYSQL_PORT, "bootstrapped": False,
    })
    connection = None
    for candidate in (root_password, ""):
        try:
            connection = mysql_connect(user="root", password=candidate, unix_socket=base / "mysql.sock")
            break
        except pymysql.err.OperationalError:
            continue
    if connection is None:
        raise RuntimeError("无法以 root 登录本地 MySQL（初始空口令与 .runtime/engines/mysql-root.json 中的口令均无效）。")
    with connection:
        with connection.cursor() as cursor:
            cursor.execute("ALTER USER 'root'@'localhost' IDENTIFIED BY %s", (root_password,))
            cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{DEMO_DB}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci")
            for host in ("127.0.0.1", "localhost"):
                cursor.execute(f"CREATE USER IF NOT EXISTS '{DEMO_USER}'@'{host}' IDENTIFIED BY %s", (password,))
                cursor.execute(f"ALTER USER '{DEMO_USER}'@'{host}' IDENTIFIED BY %s", (password,))
                cursor.execute(f"GRANT ALL PRIVILEGES ON `{DEMO_DB}`.* TO '{DEMO_USER}'@'{host}'")
            cursor.execute("FLUSH PRIVILEGES")
        connection.commit()
    config = {"type": "mysql", "host": "127.0.0.1", "port": MYSQL_PORT, "user": DEMO_USER,
              "password": password, "database": DEMO_DB}
    write_json(config_file("mysql"), config)
    write_json(RUNTIME / "mysql-root.json", {
        "user": "root", "root_password": root_password, "socket": str(base / "mysql.sock"),
        "port": MYSQL_PORT, "bootstrapped": True,
    })
    return config


def mysql_version(config: dict, *, plain: bool = False) -> str:
    connection = mysql_connect(user=config["user"], password=config["password"],
                               database=config["database"], plain=plain)
    with connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT VERSION()")
            return str(cursor.fetchone()[0])


def start_mysql() -> str:
    mysqld = mysqld_path()
    if not mysqld:
        raise EngineUnavailable("未找到 mysqld（Homebrew 可执行 brew install mysql），已跳过本地 MySQL。")
    base = engine_dir("mysql")
    (base / "tmp").mkdir(parents=True, exist_ok=True)
    base.chmod(0o700)
    if len(str(base / "mysql.sock")) > 100:
        raise RuntimeError("项目路径过长，MySQL 套接字路径超过系统限制（104 字节）。")
    mysql_initialize(mysqld)
    reused = owned_process(pid_file("mysql"), mysql_marker()) is not None
    if not reused:
        require_free_port(MYSQL_PORT)
        process = launch_process(mysqld_command(mysqld), log_path=base / "stdout.log",
                                 pid_file=pid_file("mysql"), cwd=base)
        for _ in range(90):
            if process.poll() is not None:
                raise RuntimeError(f"mysqld 提前退出，日志：{base / 'mysqld.log'}")
            if tcp_open(MYSQL_PORT) and (base / "mysql.sock").exists():
                break
            time.sleep(1)
        else:
            raise RuntimeError(f"mysqld 未在 90 秒内接受连接，日志：{base / 'mysqld.log'}")
    config = mysql_bootstrap()
    version = mysql_version(config)
    try:
        mysql_version(config, plain=True)
    except Exception as error:  # noqa: BLE001 - diagnostic only
        say(f"  ⚠ 明文 TCP 快速认证暂不可用（后端连接时请勾选 ssl）：{error}")
    note = "复用运行中的实例" if reused else "新启动"
    return f"MySQL {version} 已就绪：127.0.0.1:{MYSQL_PORT}（用户 {DEMO_USER}，数据库 {DEMO_DB}，{note}）"


# --------------------------------------------------------------------------- ClickHouse

def clickhouse_pin() -> dict:
    pin = json.loads(PIN_FILE.read_text())
    for key in ("release", "url_template", "sha256"):
        if key not in pin:
            raise RuntimeError(f"ClickHouse 版本固定文件缺少字段 {key}：{PIN_FILE}")
    return pin


def clickhouse_target(system: str | None = None, machine: str | None = None) -> str | None:
    """Official release asset suffix for this platform (only macOS assets exist on GitHub)."""
    system = system or platform.system()
    machine = machine or platform.machine()
    arch = {"arm64": "aarch64", "aarch64": "aarch64", "x86_64": "amd64", "amd64": "amd64"}.get(machine)
    if system == "Darwin":
        return {"aarch64": "macos-aarch64", "amd64": "macos"}.get(arch)
    return None


def clickhouse_download_url(pin: dict, target: str) -> str:
    return pin["url_template"].format(release=pin["release"], target=target)


CLICKHOUSE_ALIASES = ("clickhouse-watchdog",)


def clickhouse_marker() -> str:
    return f"--config-file={engine_dir('clickhouse') / 'config.xml'}"


def download_file(url: str, destination: Path, log: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".part")
    run(["curl", "-fL", "--retry", "3", "--connect-timeout", "20", url, "-o", str(temporary)], log=log, timeout=3600)
    os.replace(temporary, destination)


def ensure_clickhouse_binary() -> Path:
    pin = clickhouse_pin()
    target = clickhouse_target()
    if not target or target not in pin["sha256"]:
        raise EngineUnavailable(
            f"当前平台 {platform.system()}/{platform.machine()} 没有固定校验和的 ClickHouse 官方二进制，已跳过。")
    base = engine_dir("clickhouse")
    base.mkdir(parents=True, exist_ok=True)
    base.chmod(0o700)
    binary = base / "clickhouse"
    record_file = base / "binary.json"
    record = read_json(record_file) or {}
    expected = pin["sha256"][target]
    if binary.is_file() and record.get("release") == pin["release"] and record.get("target") == target \
            and record.get("download_sha256") == expected:
        if sha256_file(binary) == record.get("installed_sha256"):
            return binary
        raise RuntimeError(f"ClickHouse 二进制校验失败：{binary} 与安装记录不符；请删除后重试（脚本不会自动删除）。")
    archive = DOWNLOADS / f"clickhouse-{target}-{pin['release']}"
    if not archive.is_file() or sha256_file(archive) != expected:
        if archive.is_file():
            say("  已下载的 ClickHouse 文件校验和不符（官方二进制首次运行会自解压覆盖原文件），重新下载…")
        say(f"  下载官方 ClickHouse {pin['release']}（约 154 MB）…")
        download_file(clickhouse_download_url(pin, target), archive, DOWNLOADS / "clickhouse-download.log")
        if sha256_file(archive) != expected:
            raise RuntimeError(f"ClickHouse 官方二进制 SHA-256 不符：{archive}")
    staging = base / "clickhouse.new"
    shutil.copyfile(archive, staging)
    staging.chmod(0o700)
    # The official asset is a self-extracting executable: the first run replaces
    # the file with the decompressed server binary, so run it once here.
    output = run([str(staging), "--version"], cwd=base, timeout=600)
    version = pin["release"].lstrip("v").split("-")[0]
    if version not in output:
        raise RuntimeError(f"ClickHouse 二进制版本不符（期望 {version}）：{output[:200]}")
    os.replace(staging, binary)
    binary.chmod(0o700)
    write_json(record_file, {
        "release": pin["release"], "target": target, "download_sha256": expected,
        "installed_sha256": sha256_file(binary), "installed_size": binary.stat().st_size,
        "version_output": output.strip().splitlines()[0][:200] if output.strip() else "", "license": pin.get("license", ""),
    })
    return binary


def clickhouse_config_xml(base: Path) -> str:
    root = escape(str(base))
    return f"""<?xml version="1.0"?>
<!-- Generated by scripts/local-engines.py; loopback-only local demo server. -->
<clickhouse>
    <logger>
        <level>information</level>
        <log>{root}/logs/clickhouse-server.log</log>
        <errorlog>{root}/logs/clickhouse-server.err.log</errorlog>
        <size>50M</size>
        <count>3</count>
        <console>false</console>
    </logger>
    <listen_host>127.0.0.1</listen_host>
    <http_port>{CLICKHOUSE_HTTP_PORT}</http_port>
    <tcp_port>{CLICKHOUSE_TCP_PORT}</tcp_port>
    <path>{root}/data/</path>
    <tmp_path>{root}/tmp/</tmp_path>
    <user_files_path>{root}/user_files/</user_files_path>
    <format_schema_path>{root}/format_schemas/</format_schema_path>
    <user_directories>
        <users_xml>
            <path>{root}/users.xml</path>
        </users_xml>
        <local_directory>
            <path>{root}/access/</path>
        </local_directory>
    </user_directories>
    <default_profile>default</default_profile>
    <default_database>default</default_database>
    <max_server_memory_usage>1073741824</max_server_memory_usage>
    <max_concurrent_queries>32</max_concurrent_queries>
    <mark_cache_size>134217728</mark_cache_size>
    <mlock_executable>false</mlock_executable>
    <builtin_dictionaries_reload_interval>3600</builtin_dictionaries_reload_interval>
</clickhouse>
"""


def clickhouse_users_xml(default_password_sha256: str, lattice_password_sha256: str) -> str:
    return f"""<?xml version="1.0"?>
<!-- Generated by scripts/local-engines.py. Password hashes only; plaintext lives in the 0600 JSON files. -->
<clickhouse>
    <profiles>
        <default>
            <max_memory_usage>805306368</max_memory_usage>
            <max_threads>4</max_threads>
            <load_balancing>random</load_balancing>
        </default>
    </profiles>
    <users>
        <default>
            <password_sha256_hex>{default_password_sha256}</password_sha256_hex>
            <networks>
                <ip>::1</ip>
                <ip>127.0.0.1</ip>
            </networks>
            <profile>default</profile>
            <quota>default</quota>
            <access_management>1</access_management>
        </default>
        <{DEMO_USER}>
            <password_sha256_hex>{lattice_password_sha256}</password_sha256_hex>
            <networks>
                <ip>::1</ip>
                <ip>127.0.0.1</ip>
            </networks>
            <profile>default</profile>
            <quota>default</quota>
        </{DEMO_USER}>
    </users>
    <quotas>
        <default>
            <interval>
                <duration>3600</duration>
                <queries>0</queries>
                <errors>0</errors>
                <result_rows>0</result_rows>
                <read_rows>0</read_rows>
                <execution_time>0</execution_time>
            </interval>
        </default>
    </quotas>
</clickhouse>
"""


def ensure_clickhouse_config() -> dict:
    base = engine_dir("clickhouse")
    for name in ("data", "tmp", "user_files", "format_schemas", "logs", "access"):
        (base / name).mkdir(parents=True, exist_ok=True)
    config = read_config("clickhouse") or {}
    admin = read_json(RUNTIME / "clickhouse-admin.json") or {}
    password = config.get("password") or secrets.token_urlsafe(24)
    default_password = admin.get("password") or secrets.token_urlsafe(24)
    write_private(base / "config.xml", clickhouse_config_xml(base))
    write_private(base / "users.xml", clickhouse_users_xml(
        hashlib.sha256(default_password.encode()).hexdigest(),
        hashlib.sha256(password.encode()).hexdigest()))
    config = {"type": "clickhouse", "host": "127.0.0.1", "port": CLICKHOUSE_HTTP_PORT, "user": DEMO_USER,
              "password": password, "database": DEMO_DB, "secure": False}
    write_json(config_file("clickhouse"), config)
    write_json(RUNTIME / "clickhouse-admin.json", {
        "user": "default", "password": default_password, "http_port": CLICKHOUSE_HTTP_PORT,
        "tcp_port": CLICKHOUSE_TCP_PORT, "note": "管理账号，仅允许 127.0.0.1/::1 连接",
    })
    return config


def clickhouse_client(config: dict, *, database: str | None = None, timeout: int = 5):
    import clickhouse_connect

    return clickhouse_connect.get_client(host=config["host"], port=config["port"], username=config["user"],
                                         password=config["password"], database=database or "default",
                                         connect_timeout=timeout, send_receive_timeout=120)


def clickhouse_version(config: dict) -> str:
    client = clickhouse_client(config)
    try:
        return str(client.query("SELECT version()").result_rows[0][0])
    finally:
        client.close()


def start_clickhouse() -> str:
    binary = ensure_clickhouse_binary()
    config = ensure_clickhouse_config()
    base = engine_dir("clickhouse")
    reused = owned_process(pid_file("clickhouse"), clickhouse_marker(), CLICKHOUSE_ALIASES) is not None
    if not reused:
        require_free_port(CLICKHOUSE_HTTP_PORT)
        require_free_port(CLICKHOUSE_TCP_PORT)
        process = launch_process([str(binary), "server", clickhouse_marker()],
                                 log_path=base / "logs" / "stdout.log", pid_file=pid_file("clickhouse"), cwd=base)
        for _ in range(90):
            if process.poll() is not None:
                raise RuntimeError(f"ClickHouse 提前退出，日志：{base / 'logs' / 'clickhouse-server.err.log'}")
            if tcp_open(CLICKHOUSE_HTTP_PORT):
                break
            time.sleep(1)
        else:
            raise RuntimeError(f"ClickHouse 未在 90 秒内接受连接，日志：{base / 'logs' / 'clickhouse-server.err.log'}")
    last_error = ""
    for _ in range(30):
        try:
            version = clickhouse_version(config)
            break
        except Exception as error:  # noqa: BLE001 - retried until ready
            last_error = str(error)
            time.sleep(1)
    else:
        raise RuntimeError(f"ClickHouse 未通过就绪检查：{last_error}")
    note = "复用运行中的实例" if reused else "新启动"
    return (f"ClickHouse {version} 已就绪：127.0.0.1:{CLICKHOUSE_HTTP_PORT}（HTTP）/ "
            f"{CLICKHOUSE_TCP_PORT}（TCP），用户 {DEMO_USER}，{note}")


# --------------------------------------------------------------------------- PostgreSQL demo database

def polaris_database_password() -> str | None:
    credentials = read_json(POLARIS_RUNTIME / "credentials.json") or {}
    password = credentials.get("database_password")
    if not password:
        try:
            password = (POLARIS_RUNTIME / "postgres-password").read_text().strip()
        except OSError:
            return None
    return password or None


def postgres_connect(user: str, password: str, database: str, timeout: int = 5):
    import psycopg

    return psycopg.connect(host="127.0.0.1", port=PG_PORT, user=user, password=password, dbname=database,
                           connect_timeout=timeout, sslmode="disable", autocommit=True)


def ensure_postgres_demo(superuser_password: str) -> dict:
    from psycopg import sql

    config = read_config("postgres") or {}
    password = config.get("password") or secrets.token_urlsafe(24)
    with postgres_connect(PG_SUPERUSER, superuser_password, "postgres") as connection:
        role_exists = connection.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (PG_DEMO_USER,)).fetchone()
        if not role_exists:
            connection.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(PG_DEMO_USER), sql.Literal(password)))
        elif not config.get("password"):
            # Our own demo role without a private record: reset its password so the WebUI can log in.
            connection.execute(sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                sql.Identifier(PG_DEMO_USER), sql.Literal(password)))
        database_exists = connection.execute("SELECT 1 FROM pg_database WHERE datname = %s", (DEMO_DB,)).fetchone()
        if not database_exists:
            connection.execute(sql.SQL("CREATE DATABASE {} OWNER {} ENCODING 'UTF8'").format(
                sql.Identifier(DEMO_DB), sql.Identifier(PG_DEMO_USER)))
    config = {"type": "postgresql", "host": "127.0.0.1", "port": PG_PORT, "user": PG_DEMO_USER,
              "password": password, "database": DEMO_DB, "sslmode": "disable"}
    write_json(config_file("postgres"), config)
    return config


def postgres_version(config: dict) -> str:
    with postgres_connect(config["user"], config["password"], config["database"]) as connection:
        return str(connection.execute("SHOW server_version").fetchone()[0])


def start_postgres() -> str:
    try:
        import psycopg  # noqa: F401
    except ImportError as error:
        raise EngineUnavailable(f"未安装 psycopg，已跳过 PostgreSQL 示例库：{error}") from error
    password = polaris_database_password()
    if not password or not tcp_open(PG_PORT):
        raise EngineUnavailable("PostgreSQL 示例库需要先启动 Polaris（./start-web.sh）")
    config = ensure_postgres_demo(password)
    version = postgres_version(config)
    return f"PostgreSQL {version} 示例库已就绪：127.0.0.1:{PG_PORT}/{DEMO_DB}（用户 {PG_DEMO_USER}）"


# --------------------------------------------------------------------------- Paimon

def paimon_warehouse_uri() -> str:
    return "file://" + str(engine_dir("paimon") / "warehouse")


def paimon_catalog():
    from pypaimon import CatalogFactory

    return CatalogFactory.create({"warehouse": paimon_warehouse_uri()})


def start_paimon() -> str:
    try:
        import pypaimon  # noqa: F401
    except ImportError as error:
        raise EngineUnavailable(f"未安装 pypaimon，已跳过 Paimon 仓库：{error}") from error
    warehouse = engine_dir("paimon") / "warehouse"
    warehouse.mkdir(parents=True, exist_ok=True)
    engine_dir("paimon").chmod(0o700)
    paimon_catalog().create_database(DEMO_DB, ignore_if_exists=True)
    write_json(config_file("paimon"), {"type": "paimon", "catalog_type": "filesystem",
                                       "warehouse": paimon_warehouse_uri(), "database": DEMO_DB})
    return f"Paimon 文件系统仓库已就绪：{paimon_warehouse_uri()}（数据库 {DEMO_DB}）"


# --------------------------------------------------------------------------- Iceberg (Polaris)

def polaris_healthy() -> bool:
    try:
        with HTTP.open(f"http://127.0.0.1:{POLARIS_HEALTH_PORT}/q/health", timeout=3) as response:
            return response.status == 200 and json.loads(response.read()).get("status") == "UP"
    except (urllib.error.URLError, OSError, ValueError):
        return False


def iceberg_catalog(static_keys: bool = False):
    from pyiceberg.catalog import load_catalog

    credentials = read_json(POLARIS_RUNTIME / "credentials.json")
    s3 = read_json(POLARIS_RUNTIME / "local-s3.json")
    if not credentials or not s3:
        raise EngineUnavailable("Iceberg 示例表需要先启动 Polaris（./start-web.sh）")
    properties = {
        "type": "rest", "uri": credentials["base_url"] + "/api/catalog",
        "credential": credentials["client_id"] + ":" + credentials["client_secret"],
        "scope": "PRINCIPAL_ROLE:ALL", "warehouse": ICEBERG_CATALOG,
        "header.Polaris-Realm": credentials["realm"],
        "header.X-Iceberg-Access-Delegation": "vended-credentials",
        "s3.endpoint": s3["endpoint"], "s3.region": s3["region"], "s3.path-style-access": "true",
    }
    if static_keys:
        properties.pop("header.X-Iceberg-Access-Delegation")
        properties.update({"s3.access-key-id": s3["access_key"], "s3.secret-access-key": s3["secret_key"]})
    return load_catalog("polaris", **properties)


def start_iceberg() -> str:
    try:
        import pyiceberg  # noqa: F401
    except ImportError as error:
        raise EngineUnavailable(f"未安装 pyiceberg，已跳过 Iceberg 示例表：{error}") from error
    if not (POLARIS_RUNTIME / "credentials.json").is_file() or not polaris_healthy():
        raise EngineUnavailable("Iceberg 示例表需要先启动 Polaris（./start-web.sh）")
    catalog = iceberg_catalog()
    catalog.create_namespace_if_not_exists(ICEBERG_NAMESPACE)
    return (f"Apache Polaris 目录 {ICEBERG_CATALOG} 可用：http://127.0.0.1:{POLARIS_PORT}/api/catalog"
            f"（命名空间 {'.'.join(ICEBERG_NAMESPACE)}）")


# --------------------------------------------------------------------------- seeding

def seed_summary(created: list[str], existing: list[str], mismatched: dict[str, tuple[int, int]],
                 rows: int) -> dict:
    return {"created": created, "existing": existing, "mismatched": mismatched, "rows": rows}


def seed_mysql(sample: dict[str, dict]) -> dict:
    config = read_config("mysql")
    if not config:
        raise RuntimeError("缺少 .runtime/engines/mysql.json")
    created, existing, mismatched, total = [], [], {}, 0
    connection = mysql_connect(user=config["user"], password=config["password"], database=DEMO_DB)
    with connection:
        with connection.cursor() as cursor:
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
                    cursor.execute(create_table_sql("mysql", name, spec["columns"]))
                columns = ", ".join(f"`{column}`" for column, _ in spec["columns"])
                placeholders = ", ".join(["%s"] * len(spec["columns"]))
                cursor.executemany(f"INSERT INTO `{DEMO_DB}`.`{name}` ({columns}) VALUES ({placeholders})",
                                   sample_rows(spec))
                connection.commit()
                created.append(name)
                total += spec["rows"]
    return seed_summary(created, existing, mismatched, total)


def seed_postgres(sample: dict[str, dict]) -> dict:
    config = read_config("postgres")
    if not config:
        raise RuntimeError("缺少 .runtime/engines/postgres.json")
    created, existing, mismatched, total = [], [], {}, 0
    with postgres_connect(config["user"], config["password"], config["database"]) as connection:
        for name, spec in sample.items():
            with connection.transaction():
                exists = connection.execute("SELECT to_regclass(%s)", (f'public."{name}"',)).fetchone()[0]
                if exists:
                    count = int(connection.execute(f'SELECT count(*) FROM public."{name}"').fetchone()[0])
                    if count == spec["rows"]:
                        existing.append(name)
                        total += count
                        continue
                    if count:
                        mismatched[name] = (count, spec["rows"])
                        continue
                else:
                    connection.execute(create_table_sql("postgres", name, spec["columns"]))
                columns = ", ".join(f'"{column}"' for column, _ in spec["columns"])
                with connection.cursor() as cursor:
                    with cursor.copy(f'COPY public."{name}" ({columns}) FROM STDIN') as copy:
                        for row in sample_rows(spec):
                            copy.write_row(row)
                created.append(name)
                total += spec["rows"]
    return seed_summary(created, existing, mismatched, total)


def seed_clickhouse(sample: dict[str, dict]) -> dict:
    config = read_config("clickhouse")
    if not config:
        raise RuntimeError("缺少 .runtime/engines/clickhouse.json")
    created, existing, mismatched, total = [], [], {}, 0
    client = clickhouse_client(config)
    try:
        client.command(f"CREATE DATABASE IF NOT EXISTS `{DEMO_DB}`")
        for name, spec in sample.items():
            exists = client.query("SELECT count() FROM system.tables WHERE database = {db:String} AND name = {name:String}",
                                  parameters={"db": DEMO_DB, "name": name}).result_rows[0][0]
            if exists:
                count = int(client.query(f"SELECT count() FROM `{DEMO_DB}`.`{name}`").result_rows[0][0])
                if count == spec["rows"]:
                    existing.append(name)
                    total += count
                    continue
                if count:
                    mismatched[name] = (count, spec["rows"])
                    continue
            else:
                client.command(create_table_sql("clickhouse", name, spec["columns"]))
            client.insert(name, sample_rows(spec), column_names=[column for column, _ in spec["columns"]],
                          database=DEMO_DB)
            created.append(name)
            total += spec["rows"]
    finally:
        client.close()
    return seed_summary(created, existing, mismatched, total)


def paimon_row_count(table) -> int:
    builder = table.new_read_builder()
    splits = builder.new_scan().plan().splits()
    if not splits:
        return 0
    arrow = builder.new_read().to_arrow(splits)
    return arrow.num_rows if arrow is not None else 0


def seed_paimon(sample: dict[str, dict]) -> dict:
    from pypaimon import Schema

    catalog = paimon_catalog()
    catalog.create_database(DEMO_DB, ignore_if_exists=True)
    created, existing, mismatched, total = [], [], {}, 0
    for name, spec in sample.items():
        identifier = f"{DEMO_DB}.{name}"
        exists = name in set(catalog.list_tables(DEMO_DB))
        if exists:
            count = paimon_row_count(catalog.get_table(identifier))
            if count == spec["rows"]:
                existing.append(name)
                total += count
                continue
            if count:
                mismatched[name] = (count, spec["rows"])
                continue
        else:
            # Append-only tables (no primary key) keep the demo data identical to the source.
            catalog.create_table(identifier, Schema.from_pyarrow_schema(spec["arrow"].schema), ignore_if_exists=True)
        table = catalog.get_table(identifier)
        builder = table.new_batch_write_builder()
        writer = builder.new_write()
        try:
            writer.write_arrow(spec["arrow"])
            commit = builder.new_commit()
            try:
                commit.commit(writer.prepare_commit())
            finally:
                commit.close()
        finally:
            writer.close()
        created.append(name)
        total += spec["rows"]
    return seed_summary(created, existing, mismatched, total)


def iceberg_row_count(table) -> int:
    snapshot = table.current_snapshot()
    if snapshot is None:
        return 0
    summary = getattr(snapshot, "summary", None) or {}
    try:
        return int(summary["total-records"])
    except (KeyError, TypeError, ValueError):
        return table.scan().to_arrow().num_rows


def seed_iceberg(sample: dict[str, dict]) -> dict:
    catalog = iceberg_catalog()
    fallback = None
    created, existing, mismatched, total = [], [], {}, 0
    for name, spec in sample.items():
        identifier = (*ICEBERG_NAMESPACE, name)
        if catalog.table_exists(identifier):
            count = iceberg_row_count(catalog.load_table(identifier))
            if count == spec["rows"]:
                existing.append(name)
                total += count
                continue
            if count:
                mismatched[name] = (count, spec["rows"])
                continue
            table = catalog.load_table(identifier)
        else:
            table = catalog.create_table(identifier, schema=spec["arrow"].schema)
        try:
            table.append(spec["arrow"])
        except Exception as error:  # noqa: BLE001 - retry with static object-store keys
            if fallback is None:
                say(f"  ⚠ Polaris 凭据托管写入失败，改用本地对象存储静态密钥：{str(error)[:160]}")
                fallback = iceberg_catalog(static_keys=True)
            fallback.load_table(identifier).append(spec["arrow"])
        created.append(name)
        total += spec["rows"]
    return seed_summary(created, existing, mismatched, total)


def docker_log() -> Path:
    RUNTIME.mkdir(parents=True, exist_ok=True, mode=0o700)
    return RUNTIME / "docker.log"


def write_engine_config(engine: str, config: dict) -> None:
    write_json(config_file(engine), config)


def docker_starter(engine: str):
    def start() -> str:
        return docker_engines.STARTERS[engine](docker_log(), write_engine_config)

    return start


def docker_seeder(engine: str):
    def seed(sample: dict[str, dict]) -> dict:
        return docker_engines.SEEDERS[engine](sample, sample_rows, seed_summary)

    return seed


SEEDERS = {"mysql": seed_mysql, "clickhouse": seed_clickhouse, "postgres": seed_postgres,
           "paimon": seed_paimon, "iceberg": seed_iceberg,
           **{engine: docker_seeder(engine) for engine in docker_engines.DOCKER_ENGINES}}
STARTERS = {"mysql": start_mysql, "clickhouse": start_clickhouse, "postgres": start_postgres,
            "paimon": start_paimon, "iceberg": start_iceberg,
            **{engine: docker_starter(engine) for engine in docker_engines.DOCKER_ENGINES}}


def describe_seed(summary: dict) -> str:
    parts = [f"{len(summary['created']) + len(summary['existing'])} 张表共 {summary['rows']} 行"]
    if summary["created"]:
        parts.append(f"本次新建 {len(summary['created'])} 张")
    if summary["existing"]:
        parts.append(f"已存在 {len(summary['existing'])} 张")
    for name, (actual, expected) in summary["mismatched"].items():
        parts.append(f"{name} 行数 {actual} ≠ 期望 {expected}，未修改")
    return "；".join(parts)


def seed(engines: list[str]) -> list[str]:
    """Create the missing sample tables in every listed engine; returns the engines that failed."""
    if not engines:
        say("→ 没有可写入示例数据的引擎。")
        return []
    sample = load_sample()
    total = sum(spec["rows"] for spec in sample.values())
    say(f"→ 写入示例数据（{len(sample)} 张表，共 {total} 行）")
    failed = []
    for engine in engines:
        try:
            summary = SEEDERS[engine](sample)
        except EngineUnavailable as error:
            say(f"  ⚠ {LABELS[engine]}：{error}")
            continue
        except Exception as error:  # noqa: BLE001 - one engine must not block the others
            say(f"  ✗ {LABELS[engine]}：写入示例数据失败：{error}")
            failed.append(engine)
            continue
        mark = "⚠" if summary["mismatched"] else "✓"
        say(f"  {mark} {LABELS[engine]}：{describe_seed(summary)}")
    record = read_json(RUNTIME / "seed.json") or {}
    record.update({engine: time.strftime("%Y-%m-%dT%H:%M:%S") for engine in engines if engine not in failed})
    write_json(RUNTIME / "seed.json", record)
    return failed


# --------------------------------------------------------------------------- status

def probe_counts(engine: str, names: list[str]) -> tuple[dict[str, int], str]:
    """Live per-table row counts (missing tables omitted) and the server version."""
    if engine in docker_engines.DOCKER_ENGINES:
        return docker_engines.probe(engine, names, read_config(engine))
    if engine == "mysql":
        config = read_config("mysql")
        if not config:
            raise EngineUnavailable("尚未初始化")
        connection = mysql_connect(user=config["user"], password=config["password"], database=DEMO_DB, timeout=3)
        with connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT VERSION()")
                version = str(cursor.fetchone()[0])
                cursor.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = %s", (DEMO_DB,))
                present = {row[0] for row in cursor.fetchall()}
                counts = {}
                for name in names:
                    if name in present:
                        cursor.execute(f"SELECT count(*) FROM `{DEMO_DB}`.`{name}`")
                        counts[name] = int(cursor.fetchone()[0])
        return counts, version
    if engine == "clickhouse":
        config = read_config("clickhouse")
        if not config:
            raise EngineUnavailable("尚未初始化")
        client = clickhouse_client(config, timeout=3)
        try:
            version = str(client.query("SELECT version()").result_rows[0][0])
            present = {row[0] for row in client.query("SELECT name FROM system.tables WHERE database = {db:String}",
                                                     parameters={"db": DEMO_DB}).result_rows}
            counts = {name: int(client.query(f"SELECT count() FROM `{DEMO_DB}`.`{name}`").result_rows[0][0])
                      for name in names if name in present}
        finally:
            client.close()
        return counts, version
    if engine == "postgres":
        config = read_config("postgres")
        if not config:
            raise EngineUnavailable("尚未初始化")
        with postgres_connect(config["user"], config["password"], config["database"], timeout=3) as connection:
            version = str(connection.execute("SHOW server_version").fetchone()[0])
            present = {row[0] for row in connection.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'").fetchall()}
            counts = {name: int(connection.execute(f'SELECT count(*) FROM public."{name}"').fetchone()[0])
                      for name in names if name in present}
        return counts, version
    if engine == "paimon":
        if not read_config("paimon"):
            raise EngineUnavailable("尚未初始化")
        from importlib.metadata import version as package_version

        catalog = paimon_catalog()
        present = set(catalog.list_tables(DEMO_DB)) if DEMO_DB in set(catalog.list_databases()) else set()
        counts = {name: paimon_row_count(catalog.get_table(f"{DEMO_DB}.{name}")) for name in names if name in present}
        return counts, "pypaimon " + package_version("pypaimon")
    if engine == "iceberg":
        if not polaris_healthy():
            raise EngineUnavailable("Polaris 未运行")
        from importlib.metadata import version as package_version

        catalog = iceberg_catalog()
        present = {identifier[-1] for identifier in catalog.list_tables(ICEBERG_NAMESPACE)}
        counts = {name: iceberg_row_count(catalog.load_table((*ICEBERG_NAMESPACE, name)))
                  for name in names if name in present}
        return counts, "Polaris REST · pyiceberg " + package_version("pyiceberg")
    raise ValueError(engine)


def engine_installed(engine: str) -> tuple[bool, str]:
    if engine in docker_engines.DOCKER_ENGINES:
        return docker_engines.installed(engine)
    if engine == "mysql":
        mysqld = mysqld_path()
        return (True, str(mysqld)) if mysqld else (False, "未找到 mysqld")
    if engine == "clickhouse":
        target = clickhouse_target()
        try:
            pin = clickhouse_pin()
        except (OSError, ValueError, RuntimeError) as error:
            return False, f"版本固定文件无效：{error}"
        if not target or target not in pin["sha256"]:
            return False, "当前平台没有固定校验和的官方二进制"
        binary = engine_dir("clickhouse") / "clickhouse"
        return (True, f"{pin['release']} 已安装") if binary.is_file() else (True, f"{pin['release']} 尚未安装，首次启动时下载")
    if engine == "postgres":
        try:
            import psycopg  # noqa: F401
        except ImportError:
            return False, "未安装 psycopg"
        return (True, "复用 Polaris 的项目 PostgreSQL 集群") if polaris_database_password() else (False, "Polaris 尚未初始化")
    if engine == "paimon":
        try:
            import pypaimon  # noqa: F401
        except ImportError:
            return False, "未安装 pypaimon"
        return True, "pypaimon 文件系统目录"
    if engine == "iceberg":
        try:
            import pyiceberg  # noqa: F401
        except ImportError:
            return False, "未安装 pyiceberg"
        return (True, "Apache Polaris REST 目录") if (POLARIS_RUNTIME / "credentials.json").is_file() else (False, "Polaris 尚未初始化")
    raise ValueError(engine)


def engine_status(engine: str, names: list[str], expected: dict[str, int] | None) -> dict:
    installed, detail = engine_installed(engine)
    status = {"installed": installed, "running": False, "port": PORTS[engine], "seeded": False, "detail": detail}
    if not installed:
        return status
    if engine in ("mysql", "clickhouse"):
        marker = mysql_marker() if engine == "mysql" else clickhouse_marker()
        aliases = CLICKHOUSE_ALIASES if engine == "clickhouse" else ()
        owned = owned_process(pid_file(engine), marker, aliases)
        status["pid"] = owned["pid"] if owned else None
        if not owned and not tcp_open(PORTS[engine], 0.5):
            status["detail"] = f"{detail}；未运行"
            return status
    if engine in docker_engines.DOCKER_ENGINES:
        status["containers"] = list(docker_engines.CONTAINERS[engine])
        if not docker_engines.running(engine):
            status["detail"] = f"{detail}；容器未运行"
            return status
    try:
        counts, version = probe_counts(engine, names)
    except EngineUnavailable as error:
        status["detail"] = f"{detail}；{error}"
        return status
    except Exception as error:  # noqa: BLE001 - reported in the status document
        status["detail"] = f"{detail}；连接失败：{str(error)[:160]}"
        return status
    status["running"] = True
    status["version"] = version
    status["tables"] = counts
    seeded = bool(expected) and all(counts.get(name) == rows for name, rows in expected.items())
    status["seeded"] = seeded
    rows = sum(counts.values())
    status["detail"] = (f"{version} · {len(counts)}/{len(names)} 张示例表 · {rows} 行"
                        + ("" if seeded else " · 示例数据不完整"))
    return status


def status_report() -> dict:
    names = sample_table_names()
    try:
        expected = expected_counts()
    except (RuntimeError, ImportError):
        expected = None
    return {"engines": {engine: engine_status(engine, names, expected) for engine in ENGINES}}


def running_engines() -> list[str]:
    report = status_report()
    return [engine for engine in ENGINES if report["engines"][engine]["running"]]


# --------------------------------------------------------------------------- quality datasource

QUALITY_DATASOURCE = "quality-datasource"
# The PostgreSQL connector only offers these three; any other mode libpq accepts
# would render as an empty select in the WebUI.
SSL_MODES = ("disable", "prefer", "require")


def quality_datasource_config() -> dict:
    """Translate the data quality module's DSN into a PostgreSQL connector config.

    ``quality-postgres`` is the one builtin source this script does not start: it
    is the existing business database the quality module already talks to.  Its
    connection details are therefore read from ``webapi.quality_store`` rather
    than from a server we launched, so the DSN and its ``LATTICE_QUALITY_DSN``
    override keep a single definition.
    """
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    try:
        from psycopg.conninfo import conninfo_to_dict  # noqa: WPS433 (project module)
        from webapi import env
        from webapi.quality_store import DEFAULT_DSN, DSN_SETTING, parse_dsn
    except ImportError as error:
        raise RuntimeError(f"无法导入 webapi.quality_store（质量元数据库连接串来源）：{error}") from error
    parts = conninfo_to_dict(parse_dsn(env(DSN_SETTING) or DEFAULT_DSN))
    missing = [key for key in ("host", "user", "dbname") if not parts.get(key)]
    if missing:
        raise ValueError("质量元数据库连接串缺少 " + "、".join(missing))
    config = {
        "type": "postgresql",
        "host": str(parts["host"]),
        "port": int(parts.get("port") or 5432),
        "user": str(parts["user"]),
        "password": str(parts.get("password") or ""),
        "database": str(parts["dbname"]),
    }
    if str(parts.get("sslmode") or "") in SSL_MODES:
        config["sslmode"] = str(parts["sslmode"])
    return config


def write_quality_datasource() -> None:
    """Provision the builtin ``quality-postgres`` datasource config.

    webapi/datasources.py only renders that card when this file exists, and no
    engine starter writes it, so a rebuilt ``.runtime`` used to drop the card
    from the datasource page altogether.  It is written even when the database
    is unreachable: a stopped database should show the card offline, not make it
    disappear.
    """
    say("→ 数据质量业务库（PostgreSQL）")
    try:
        config = quality_datasource_config()
    except (RuntimeError, ValueError) as error:
        say(f"  ⚠ 未写入数据源配置：{error}")
        return
    write_engine_config(QUALITY_DATASOURCE, config)
    say(f"  ✓ 数据源配置已写入（{config['host']}:{config['port']}/{config['database']}）")


# --------------------------------------------------------------------------- commands

def start() -> int:
    failed, available = [], []
    write_quality_datasource()
    for engine in ENGINES:
        port = PORTS[engine]
        say(f"→ 本地 {LABELS[engine]}" + (f"（127.0.0.1:{port}）" if port else ""))
        try:
            say("  ✓ " + STARTERS[engine]())
            available.append(engine)
        except EngineUnavailable as error:
            say(f"  ⚠ {error}")
        except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
            say(f"  ✗ {LABELS[engine]} 启动失败：{error}")
            failed.append(engine)
        except Exception as error:  # noqa: BLE001 - driver exceptions must not abort the other engines
            say(f"  ✗ {LABELS[engine]} 启动失败：{type(error).__name__}: {error}")
            failed.append(engine)
    failed += seed(available)
    if failed:
        say("本地示例引擎未全部就绪：" + "、".join(LABELS[engine] for engine in failed))
        return 1
    say("本地示例引擎已就绪。")
    return 0


def stop() -> None:
    for engine, marker, aliases in (("clickhouse", clickhouse_marker(), CLICKHOUSE_ALIASES),
                                    ("mysql", mysql_marker(), ())):
        record = owned_process(pid_file(engine), marker, aliases)
        if record:
            say(f"→ 停止本地 {LABELS[engine]}（PID {record['pid']}）")
            stop_process(pid_file(engine), marker, aliases=aliases)
            say(f"  ✓ {LABELS[engine]} 已停止，数据保留")
        elif pid_file(engine).exists():
            pid_file(engine).unlink(missing_ok=True)
            say(f"  {LABELS[engine]} 的 PID 记录已过期，已清理；未终止任何无关进程")
        else:
            say(f"  本地 {LABELS[engine]} 未运行")
    for engine in docker_engines.DOCKER_ENGINES:
        docker_engines.stop(engine, say)
    say("PostgreSQL 示例库由 Polaris 启动器管理，未停止；Paimon / Iceberg 无进程。")


def main() -> int:
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    command = sys.argv[1] if len(sys.argv) > 1 else "start"
    if len(sys.argv) > 2 or command not in {"start", "stop", "restart", "status", "seed"}:
        raise RuntimeError("用法：local-engines.py [start|stop|restart|status|seed]")
    if not shutil.which("ps"):
        raise RuntimeError("需要 ps 命令来验证进程所有权。")
    os.umask(0o077)
    RUNTIME.mkdir(parents=True, exist_ok=True, mode=0o700)
    RUNTIME.chmod(0o700)
    with (RUNTIME / "control.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            say("另一个本地引擎管理任务正在运行，等待其完成…")
            fcntl.flock(lock, fcntl.LOCK_EX)
        if command == "status":
            print(json.dumps(status_report(), ensure_ascii=False, indent=2))
            return 0
        if command in {"stop", "restart"}:
            stop()
        if command in {"start", "restart"}:
            return start()
        if command == "seed":
            return 1 if seed(running_engines()) else 0
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("本地引擎操作已中断。", file=sys.stderr)
        sys.exit(130)
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"本地引擎操作失败：{error}", file=sys.stderr)
        sys.exit(1)
