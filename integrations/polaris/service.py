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
"""Run the verified upstream distribution with a project-owned PostgreSQL cluster."""

from __future__ import annotations

import fcntl
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import platform
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / ".runtime" / "polaris"
PIN = json.loads((Path(__file__).parent / "upstream.json").read_text())
DIST = RUNTIME / f"polaris-bin-{PIN['version']}"
PGDATA = RUNTIME / "postgres"
API_PORT = 8181
HEALTH_PORT = 8182
PG_PORT = 55432
BASE_URL = f"http://127.0.0.1:{API_PORT}"
PID_FILE = RUNTIME / "server.json"
MINIO_PID = RUNTIME / "minio.json"
MINIO_BIN = RUNTIME / "minio"
S3_PORT = 19000
S3_CONSOLE_PORT = 19001
S3_URL = f"http://127.0.0.1:{S3_PORT}"
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def write_private(path: Path, content: str) -> None:
    """Replace a private file atomically without following an existing symlink."""
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(content)
    os.replace(temporary, path)


def run(command: list[str], *, log: str | None = None, env: dict | None = None) -> str:
    if log:
        with (RUNTIME / log).open("a") as stream:
            result = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, env=env)
        if result.returncode:
            raise RuntimeError(f"Command failed ({result.returncode}); see {RUNTIME / log}")
        return ""
    return subprocess.check_output(command, text=True, stderr=subprocess.STDOUT, env=env).strip()


def pg_bin() -> Path:
    # Persist the binary directory so a future Homebrew upgrade cannot silently
    # switch an existing PostgreSQL data directory to an incompatible major.
    saved = RUNTIME / "postgres-bin.txt"
    if saved.exists() and (PGDATA / "PG_VERSION").exists():
        candidate = Path(saved.read_text().strip())
        if (candidate / "pg_ctl").is_file():
            return candidate
        raise RuntimeError(f"PostgreSQL binary disappeared: {candidate}. Restore its original major version.")
    configured = os.environ.get("LATTICE_PG_BIN", "/nonexistent")
    candidates = [Path(configured)]
    candidates += [Path(p) for p in (
        "/opt/homebrew/opt/postgresql@18/bin", "/opt/homebrew/opt/postgresql@16/bin",
        "/opt/homebrew/opt/postgresql@15/bin", "/usr/local/opt/postgresql@18/bin",
        "/usr/local/opt/postgresql@16/bin",
    )]
    found = shutil.which("pg_ctl")
    if found:
        candidates.append(Path(found).parent)
    candidates += sorted(Path("/usr/lib/postgresql").glob("*/bin"), reverse=True)
    for candidate in candidates:
        if all((candidate / tool).is_file() for tool in ("postgres", "pg_ctl", "initdb", "createdb")):
            try:
                share_dir = Path(run([str(candidate / "pg_config"), "--sharedir"]))
                if not (share_dir / "postgres.bki").is_file():
                    continue
            except (OSError, subprocess.SubprocessError):
                continue
            write_private(saved, str(candidate) + "\n")
            return candidate
    raise RuntimeError("PostgreSQL binaries are required. Install PostgreSQL 15+ or set LATTICE_PG_BIN.")


def require_free_port(port: int) -> None:
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as exc:
            raise RuntimeError(f"Port {port} is already in use; no existing process was stopped.") from exc


def process_identity(pid: int) -> str:
    try:
        return run(["ps", "-p", str(pid), "-o", "lstart=", "-o", "command="])
    except subprocess.CalledProcessError:
        return ""


def launch_process(command: list[str], *, log_path: Path, pid_file: Path, env: dict | None = None) -> subprocess.Popen:
    """Keep the child handle until its ownership record has been committed."""
    process = None
    try:
        with log_path.open("a") as log:
            process = subprocess.Popen(command, cwd=RUNTIME, env=env, stdin=subprocess.DEVNULL,
                                       stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        identity = process_identity(process.pid)
        if not identity or process.poll() is not None:
            raise RuntimeError(f"Service exited before initialization; see {log_path}")
        write_private(pid_file, json.dumps({"pid": process.pid, "identity": identity}))
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


def owned_process(pid_file: Path, marker: str) -> dict | None:
    if not pid_file.exists():
        return None
    try:
        state = json.loads(pid_file.read_text())
        identity = process_identity(int(state["pid"]))
        if identity and identity == state["identity"] and marker in identity:
            return state
    except (ValueError, KeyError, TypeError):
        pass
    return None


def owned_server() -> dict | None:
    return owned_process(PID_FILE, str(DIST / "server" / "quarkus-run.jar"))


def owned_minio() -> dict | None:
    return owned_process(MINIO_PID, str(MINIO_BIN))


def credentials() -> dict:
    return json.loads((RUNTIME / "credentials.json").read_text())


def request_json(path: str, *, data: bytes | None = None, headers: dict | None = None, health=False, method: str | None = None) -> dict:
    base = f"http://127.0.0.1:{HEALTH_PORT}" if health else BASE_URL
    request = urllib.request.Request(base + path, data=data, headers=headers or {}, method=method)
    with HTTP.open(request, timeout=5) as response:
        payload = response.read()
        return json.loads(payload) if payload else {}


def api_headers() -> dict:
    auth = credentials()
    token = request_json("/api/catalog/v1/oauth/tokens", data=urllib.parse.urlencode({
        "grant_type": "client_credentials", "client_id": auth["client_id"],
        "client_secret": auth["client_secret"], "scope": "PRINCIPAL_ROLE:ALL",
    }).encode(), headers={"Content-Type": "application/x-www-form-urlencoded", "Polaris-Realm": auth["realm"]})
    return {"Authorization": "Bearer " + token["access_token"], "Polaris-Realm": auth["realm"], "Content-Type": "application/json"}


def ensure_distribution() -> None:
    if (DIST / "server" / "quarkus-run.jar").is_file() and (DIST / "admin" / "quarkus-run.jar").is_file():
        return
    downloads = RUNTIME / "downloads"
    downloads.mkdir(exist_ok=True)
    archive = downloads / f"polaris-bin-{PIN['version']}.tgz"
    if not archive.exists():
        temporary = archive.with_suffix(".part")
        print(f"Downloading official Apache Polaris {PIN['version']} binary…", flush=True)
        run(["curl", "-fL", "--retry", "3", "--connect-timeout", "20", PIN["binary_url"], "-o", str(temporary)], log="download.log")
        temporary.replace(archive)
    digest = hashlib.sha512()
    with archive.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != PIN["sha512"]:
        raise RuntimeError(f"Official binary SHA-512 mismatch: {archive}; remove it and retry.")
    with tarfile.open(archive) as bundle:
        # The official archive is checksum-pinned; also reject path traversal.
        for member in bundle.getmembers():
            target = (RUNTIME / member.name).resolve()
            if not target.is_relative_to(RUNTIME.resolve()):
                raise RuntimeError("Unsafe archive path")
        bundle.extractall(RUNTIME, filter="data")


def prepare_config() -> None:
    auth_file = RUNTIME / "credentials.json"
    if not auth_file.exists():
        if (PGDATA / "PG_VERSION").exists():
            raise RuntimeError("Existing metadata has no credentials.json; restore credentials from backup.")
        write_private(auth_file, json.dumps({
            "client_id": "lattice-admin-" + secrets.token_hex(8),
            "client_secret": secrets.token_urlsafe(48), "realm": "LATTICE",
            "base_url": BASE_URL, "database_password": secrets.token_urlsafe(40),
        }, indent=2) + "\n")
    auth = credentials()
    write_private(RUNTIME / "bootstrap.json", json.dumps({auth["realm"]: {
        "client-id": auth["client_id"], "client-secret": auth["client_secret"],
    }}))
    write_private(RUNTIME / "postgres-password", auth["database_password"])
    private_key = RUNTIME / "token-private.pem"
    public_key = RUNTIME / "token-public.pem"
    if not private_key.exists():
        run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(private_key)], log="keygen.log")
    if not public_key.exists():
        run(["openssl", "pkey", "-in", str(private_key), "-pubout", "-out", str(public_key)], log="keygen.log")
    private_key.chmod(0o600)
    public_key.chmod(0o600)
    warehouse = RUNTIME / "warehouse"
    warehouse.mkdir(exist_ok=True)
    properties = {
        "polaris.persistence.type": "relational-jdbc",
        "quarkus.datasource.jdbc.url": f"jdbc:postgresql://127.0.0.1:{PG_PORT}/lattice_polaris",
        "quarkus.datasource.username": "lattice_polaris",
        "quarkus.datasource.password": auth["database_password"],
        "polaris.realm-context.realms": auth["realm"],
        "polaris.realm-context.require-header": "true",
        "polaris.authentication.token-broker.rsa-key-pair.private-key-file": str(private_key),
        "polaris.authentication.token-broker.rsa-key-pair.public-key-file": str(public_key),
        'polaris.features."ENABLE_SEMANTIC_MODELS"': "true",
        'polaris.features."ENABLE_GENERIC_TABLES"': "true",
        'polaris.features."ENABLE_POLICY_STORE"': "true",
        'polaris.features."ENABLE_CATALOG_FEDERATION"': "true",
        'polaris.features."SUPPORTED_CATALOG_STORAGE_TYPES"': '["S3","GCS","AZURE"]',
        "quarkus.log.file.path": str(RUNTIME / "polaris.log"),
        "quarkus.http.access-log.enabled": "false",
    }
    content = "# Generated local configuration; edit overrides.properties for upstream feature options.\n"
    content += "\n".join(f"{key}={value}" for key, value in properties.items()) + "\n"
    overrides = RUNTIME / "overrides.properties"
    if overrides.exists():
        content += "\n# User overrides\n" + overrides.read_text() + "\n"
    write_private(RUNTIME / "application.properties", content)


def minio_credentials() -> dict:
    return json.loads((RUNTIME / "local-s3.json").read_text())


def ensure_minio_binary() -> None:
    pin = json.loads((Path(__file__).parent / "minio.json").read_text())
    arch = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "amd64"}.get(platform.machine())
    target = f"{platform.system().lower()}-{arch}"
    if target not in pin["sha256"]:
        raise RuntimeError(f"No pinned local MinIO binary for {target}")
    archive = RUNTIME / "downloads" / f"minio-{target}"
    if not MINIO_BIN.exists():
        if not archive.exists():
            url = f"https://github.com/minio/minio/releases/download/{pin['release']}/minio.{target}.{pin['release']}"
            temporary = archive.with_suffix(".part")
            print("Downloading official MinIO local object store…", flush=True)
            run(["curl", "-fL", "--retry", "3", "--connect-timeout", "20", url, "-o", str(temporary)], log="download.log")
            temporary.replace(archive)
        shutil.copyfile(archive, MINIO_BIN)
        MINIO_BIN.chmod(0o700)
    digest = hashlib.sha256(MINIO_BIN.read_bytes()).hexdigest()
    if digest != pin["sha256"][target]:
        raise RuntimeError(f"MinIO SHA-256 mismatch: {MINIO_BIN}")


def s3_health() -> bool:
    try:
        with HTTP.open(S3_URL + "/minio/health/ready", timeout=3) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError):
        return False


def create_bucket() -> None:
    """Create the private local bucket using the standard S3 Signature V4 protocol."""
    auth = minio_credentials()
    now = datetime.now(timezone.utc)
    stamp, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    host = f"127.0.0.1:{S3_PORT}"
    body_hash = hashlib.sha256(b"").hexdigest()
    canonical_headers = f"host:{host}\nx-amz-content-sha256:{body_hash}\nx-amz-date:{stamp}\n"
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    path = "/" + auth["bucket"]
    canonical = f"PUT\n{path}\n\n{canonical_headers}\n{signed_headers}\n{body_hash}"
    scope = f"{day}/{auth['region']}/s3/aws4_request"
    string_to_sign = f"AWS4-HMAC-SHA256\n{stamp}\n{scope}\n{hashlib.sha256(canonical.encode()).hexdigest()}"
    key = ("AWS4" + auth["secret_key"]).encode()
    for part in (day, auth["region"], "s3", "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    authorization = f"AWS4-HMAC-SHA256 Credential={auth['access_key']}/{scope}, SignedHeaders={signed_headers}, Signature={signature}"
    request = urllib.request.Request(S3_URL + path, method="PUT", data=b"", headers={
        "Host": host, "X-Amz-Date": stamp, "X-Amz-Content-Sha256": body_hash, "Authorization": authorization,
    })
    try:
        with HTTP.open(request, timeout=10) as response:
            response.read()
    except urllib.error.HTTPError as error:
        body = error.read()
        if error.code != 409 or b"BucketAlreadyOwnedByYou" not in body:
            raise RuntimeError(f"Local S3 bucket initialization failed: HTTP {error.code}") from error


def ensure_minio() -> None:
    ensure_minio_binary()
    auth_file = RUNTIME / "local-s3.json"
    if not auth_file.exists():
        write_private(auth_file, json.dumps({
            "access_key": "lattice-" + secrets.token_hex(10), "secret_key": secrets.token_urlsafe(40),
            "endpoint": S3_URL, "region": "us-east-1", "bucket": "lattice-warehouse",
        }, indent=2) + "\n")
    if not owned_minio():
        require_free_port(S3_PORT)
        require_free_port(S3_CONSOLE_PORT)
        auth = minio_credentials()
        data = RUNTIME / "object-store"
        data.mkdir(exist_ok=True)
        env = {**os.environ, "MINIO_ROOT_USER": auth["access_key"], "MINIO_ROOT_PASSWORD": auth["secret_key"],
               "MINIO_REGION_NAME": auth["region"], "MINIO_BROWSER": "off", "MINIO_UPDATE": "off"}
        command = [str(MINIO_BIN), "server", str(data), "--address", f"127.0.0.1:{S3_PORT}",
                   "--console-address", f"127.0.0.1:{S3_CONSOLE_PORT}", "--quiet"]
        launch_process(command, log_path=RUNTIME / "minio.log", pid_file=MINIO_PID, env=env)
    for _ in range(45):
        if s3_health():
            create_bucket()
            return
        time.sleep(1)
    raise RuntimeError(f"Local MinIO did not become ready; see {RUNTIME / 'minio.log'}")


def database_running() -> bool:
    return database_identity() is not None


def database_identity() -> dict | None:
    pid_file = PGDATA / "postmaster.pid"
    if not (PGDATA / "PG_VERSION").exists() or not pid_file.exists():
        return None
    lines = pid_file.read_text().splitlines()
    if len(lines) < 3:
        raise RuntimeError("Invalid project PostgreSQL PID file; no process was stopped")
    pid = int(lines[0])
    if pid <= 0 or Path(lines[1]).resolve() != PGDATA.resolve():
        raise RuntimeError("Invalid project PostgreSQL PID/data directory; no process was stopped")
    identity = process_identity(pid)
    if not identity:
        return None
    ps_env = {**os.environ, "LC_ALL": "C"}
    # Linux ps comm is often only the truncated process name, not its executable.
    executable = (os.readlink(f"/proc/{pid}/exe") if platform.system() == "Linux"
                  else run(["ps", "-p", str(pid), "-o", "comm="], env=ps_env))
    command = run(["ps", "-p", str(pid), "-o", "command="], env=ps_env)
    started = run(["ps", "-p", str(pid), "-o", "lstart="], env=ps_env)
    # ps does not zero-pad the day ("Oct  1"), so compare parsed times instead of strings.
    try:
        started_at = datetime.strptime(" ".join(started.split()), "%a %b %d %H:%M:%S %Y")
    except ValueError:
        started_at = None
    same_start = started_at is not None and abs(started_at.timestamp() - int(lines[2])) <= 2
    if (Path(executable).resolve() != (pg_bin() / "postgres").resolve()
            or not command.endswith(" -D " + str(PGDATA)) or not same_start):
        raise RuntimeError("PostgreSQL PID record does not match the project process; no process was stopped")
    return {"pid": pid, "identity": identity}


def ensure_database() -> None:
    binaries = pg_bin()
    auth = credentials()
    if not (PGDATA / "PG_VERSION").exists():
        share_dirs = [binaries.parent / "share"] + list((binaries.parent / "share").glob("postgresql*"))
        share_dir = next((path for path in share_dirs if (path / "postgres.bki").is_file()), None)
        share_args = ["-L", str(share_dir)] if share_dir else []
        run([str(binaries / "initdb"), "-D", str(PGDATA), "-U", "lattice_polaris",
             "--pwfile=" + str(RUNTIME / "postgres-password"), "--auth-local=scram-sha-256",
             "--auth-host=scram-sha-256", "--encoding=UTF8", "--locale=C", *share_args], log="postgres-init.log")
        with (PGDATA / "postgresql.conf").open("a") as stream:
            stream.write(f"\nlisten_addresses = '127.0.0.1'\nport = {PG_PORT}\nunix_socket_directories = ''\n")
    if not database_running():
        require_free_port(PG_PORT)
        run([str(binaries / "pg_ctl"), "start", "-D", str(PGDATA), "-l", str(RUNTIME / "postgres.log"), "-w", "-t", "45"], log="postgres-control.log")
    env = {**os.environ, "PGPASSWORD": auth["database_password"], "PGCONNECT_TIMEOUT": "5"}
    args = ["-h", "127.0.0.1", "-p", str(PG_PORT), "-U", "lattice_polaris"]
    exists = run([str(binaries / "psql"), *args, "-d", "postgres", "-Atc", "SELECT 1 FROM pg_database WHERE datname = 'lattice_polaris'"], env=env)
    if exists != "1":
        run([str(binaries / "createdb"), *args, "lattice_polaris"], env=env)


def java_command(component: str) -> list[str]:
    java = Path(os.environ.get("JAVA_HOME", "/nonexistent")) / "bin" / "java"
    if not java.is_file():
        raise RuntimeError("JAVA_HOME must point to JDK 21+; use scripts/polaris-service.sh.")
    return [str(java), "-Xms128m", "-Xmx1024m", "-Dquarkus.config.locations=" + (RUNTIME / "application.properties").as_uri(),
            "-Dquarkus.http.host=127.0.0.1", "-Dquarkus.management.host=127.0.0.1",
            f"-Dquarkus.http.port={API_PORT}", f"-Dquarkus.management.port={HEALTH_PORT}",
            "-jar", str(DIST / component / "quarkus-run.jar")]


def verify() -> dict:
    health = request_json("/q/health", health=True)
    if health.get("status") != "UP":
        raise RuntimeError("Polaris reports unhealthy status")
    catalogs = request_json("/api/management/v1/catalogs", headers=api_headers())
    if not s3_health():
        raise RuntimeError("Local S3 object store is unhealthy")
    return {"status": "UP", "version": PIN["version"], "base_url": BASE_URL,
            "health_url": f"http://127.0.0.1:{HEALTH_PORT}/q/health", "persistence": "PostgreSQL",
            "database_port": PG_PORT, "object_store": "MinIO", "s3_endpoint": S3_URL,
            "catalogs": [item["name"] for item in catalogs.get("catalogs", [])]}


def seed_catalog() -> None:
    headers = api_headers()
    catalogs = request_json("/api/management/v1/catalogs", headers=headers)
    if not any(item["name"] == "lattice" for item in catalogs.get("catalogs", [])):
        catalog = {"catalog": {"name": "lattice", "type": "INTERNAL", "properties": {
            "default-base-location": "s3://lattice-warehouse/lattice", "polaris.config.drop-with-purge.enabled": "true",
        }, "storageConfigInfo": {"storageType": "S3", "allowedLocations": ["s3://lattice-warehouse/lattice"],
            "endpoint": S3_URL, "endpointInternal": S3_URL, "pathStyleAccess": True, "region": "us-east-1",
        }}}
        request_json("/api/management/v1/catalogs", data=json.dumps(catalog).encode(), headers=headers)
    request_json("/api/management/v1/catalogs/lattice/catalog-roles/catalog_admin/grants", method="PUT",
                 data=json.dumps({"type": "catalog", "privilege": "CATALOG_MANAGE_CONTENT"}).encode(), headers=headers)
    namespaces = request_json("/api/catalog/v1/lattice/namespaces", headers=headers)
    if ["demo"] not in namespaces.get("namespaces", []):
        request_json("/api/catalog/v1/lattice/namespaces", data=json.dumps({"namespace": ["demo"]}).encode(), headers=headers)


def start(receipt: Path | None = None) -> None:
    if owned_server():
        result = verify()
        if receipt:
            write_private(receipt, json.dumps({"server": None, "minio": None, "database": None}))
        print(json.dumps(result, indent=2))
        return
    require_free_port(API_PORT)
    require_free_port(HEALTH_PORT)
    ensure_distribution()
    prepare_config()
    had_database, had_minio = database_running(), bool(owned_minio())
    try:
        ensure_database()
        ensure_minio()
        run([*java_command("admin"), "bootstrap", "--credentials-file", str(RUNTIME / "bootstrap.json")], log="bootstrap.log")
        auth = minio_credentials()
        env = {**os.environ, "AWS_ACCESS_KEY_ID": auth["access_key"], "AWS_SECRET_ACCESS_KEY": auth["secret_key"],
               "AWS_REGION": auth["region"], "AWS_EC2_METADATA_DISABLED": "true"}
        # Never inherit a stale session token when providing this project's static credentials.
        env.pop("AWS_SESSION_TOKEN", None)
        env_file = RUNTIME / "server-env.json"
        if env_file.exists():
            overrides = json.loads(env_file.read_text())
            if not isinstance(overrides, dict) or any(
                not isinstance(key, str) or not (value is None or isinstance(value, str))
                for key, value in overrides.items()
            ):
                raise RuntimeError("server-env.json must be an object with string or null values")
            for key, value in overrides.items():
                if value is None:
                    env.pop(key, None)
                else:
                    env[key] = value
        process = launch_process(java_command("server"), log_path=RUNTIME / "server.log", pid_file=PID_FILE, env=env)
        for _ in range(90):
            if process.poll() is not None:
                raise RuntimeError(f"Polaris exited; see {RUNTIME / 'server.log'}")
            try:
                verify()
                break
            except (urllib.error.URLError, OSError, ValueError, RuntimeError):
                time.sleep(1)
        else:
            raise RuntimeError(f"Polaris did not become ready; see {RUNTIME / 'server.log'}")
        seed_catalog()
        result = verify()
        if receipt:
            write_private(receipt, json.dumps({
                "server": owned_server(), "minio": None if had_minio else owned_minio(),
                "database": None if had_database else database_identity(),
            }))
        print(json.dumps(result, indent=2))
    except BaseException:
        stop_process(PID_FILE, str(DIST / "server" / "quarkus-run.jar"))
        if not had_minio:
            stop_process(MINIO_PID, str(MINIO_BIN))
        if not had_database and database_running():
            stop_database()
        raise


def stop_process(pid_file: Path, marker: str) -> None:
    state = owned_process(pid_file, marker)
    if state:
        os.kill(state["pid"], signal.SIGTERM)
        for _ in range(45):
            if not owned_process(pid_file, marker):
                break
            time.sleep(1)
        else:
            raise RuntimeError("Process did not stop; refused to force-kill it.")
    pid_file.unlink(missing_ok=True)


def stop_database() -> None:
    if not database_identity():
        return
    run([str(pg_bin() / "pg_ctl"), "stop", "-D", str(PGDATA), "-m", "fast", "-w", "-t", "45"], log="postgres-control.log")


def rollback(receipt: Path) -> None:
    if not receipt.exists():
        return
    state = json.loads(receipt.read_text())
    if state.get("server") and owned_server() == state["server"]:
        stop_process(PID_FILE, str(DIST / "server" / "quarkus-run.jar"))
    # A replacement server might have reused the dependencies. Preserve those.
    if not owned_server():
        if state.get("minio") and owned_minio() == state["minio"]:
            stop_process(MINIO_PID, str(MINIO_BIN))
        if state.get("database") and database_identity() == state["database"]:
            stop_database()
    receipt.unlink()


def stop() -> None:
    if not owned_server():
        # A lost/stale record must not let a potentially live service lose its dependencies.
        require_free_port(API_PORT)
        require_free_port(HEALTH_PORT)
    stop_process(PID_FILE, str(DIST / "server" / "quarkus-run.jar"))
    stop_process(MINIO_PID, str(MINIO_BIN))
    if database_running():
        stop_database()
    print("Project Polaris, PostgreSQL and MinIO stopped; data retained.")


def main() -> None:
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    os.umask(0o077)
    RUNTIME.mkdir(parents=True, exist_ok=True, mode=0o700)
    RUNTIME.chmod(0o700)
    command = sys.argv[1] if len(sys.argv) > 1 else "start"
    with (RUNTIME / "control.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if command == "start":
            if len(sys.argv) == 4 and sys.argv[2] == "--receipt":
                start(Path(sys.argv[3]))
            elif len(sys.argv) == 2 or len(sys.argv) == 1:
                start()
            else:
                raise RuntimeError("Usage: start [--receipt ABS_PATH]")
        elif command == "stop":
            stop()
        elif command == "restart":
            stop()
            start()
        elif command == "status":
            if not owned_server():
                print(json.dumps({"status": "STOPPED", "base_url": BASE_URL}))
                sys.exit(1)
            print(json.dumps(verify(), indent=2))
        elif command == "rollback" and len(sys.argv) == 3:
            rollback(Path(sys.argv[2]))
        elif command == "admin":
            if not (RUNTIME / "application.properties").exists():
                raise RuntimeError("Run start once before using the upstream admin tool.")
            subprocess.run([*java_command("admin"), *sys.argv[2:]], check=True)
        else:
            raise RuntimeError("Usage: scripts/polaris-service.sh {start|stop|restart|status|admin [args…]}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Polaris: interrupted.", file=sys.stderr)
        sys.exit(130)
    except (RuntimeError, OSError, subprocess.SubprocessError, ValueError) as error:
        print(f"Polaris: {error}", file=sys.stderr)
        sys.exit(1)
