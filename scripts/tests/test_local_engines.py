# SPDX-License-Identifier: Apache-2.0
"""Ownership, pinning and DDL rules of the local demo engine managers."""

import importlib.util
import json
from pathlib import Path
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

import lattice_docker_engines as docker_engines  # noqa: E402

SPEC = importlib.util.spec_from_file_location("local_engines", SCRIPTS / "local-engines.py")
engines = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(engines)


# ----- process ownership ------------------------------------------------------------
def write_record(path: Path, pid: int, identity: str) -> None:
    path.write_text(json.dumps({"pid": pid, "identity": identity, "root": str(ROOT)}))


LAUNCHED = "Fri Sep 11 04:09:48 2026     /project/.runtime/engines/clickhouse/clickhouse server --config-file=/project/c.xml"


@pytest.fixture
def record(tmp_path, monkeypatch):
    path = tmp_path / "clickhouse.pid.json"
    write_record(path, 4242, LAUNCHED)

    def set_live(identity):
        monkeypatch.setattr(engines, "process_identity", lambda pid: identity if pid == 4242 else "")

    return path, set_live


def test_process_renamed_after_launch_is_still_recognised_as_ours(record, monkeypatch):
    path, set_live = record
    set_live("Fri Sep 11 04:09:48 2026     clickhouse-watchdog  ")
    monkeypatch.setattr(engines, "child_carries_marker", lambda pid, marker: True)
    assert engines.owned_process(path, "--config-file=", engines.CLICKHOUSE_ALIASES)["pid"] == 4242
    # Without the documented alias the record must not match.
    assert engines.owned_process(path, "--config-file=") is None


def test_renamed_process_without_our_child_is_not_claimed(record, monkeypatch):
    """A bare "clickhouse-watchdog" says nothing about this project; a child must prove it."""
    path, set_live = record
    set_live("Fri Sep 11 04:09:48 2026     clickhouse-watchdog  ")
    monkeypatch.setattr(engines, "child_carries_marker", lambda pid, marker: False)
    assert engines.owned_process(path, "--config-file=", engines.CLICKHOUSE_ALIASES) is None


def test_child_marker_scan_matches_only_direct_children(monkeypatch):
    class Result:
        returncode = 0
        stdout = " 4242 /project/clickhouse server --config-file=/project/c.xml\n    1 /sbin/launchd\n"
        stderr = ""

    monkeypatch.setattr(engines.subprocess, "run", lambda *a, **k: Result())
    assert engines.child_carries_marker(4242, "--config-file=") is True
    assert engines.child_carries_marker(9999, "--config-file=") is False


def test_recycled_pid_with_a_different_start_time_is_never_claimed(record, monkeypatch):
    path, set_live = record
    monkeypatch.setattr(engines, "child_carries_marker", lambda pid, marker: True)
    set_live("Sat Sep 12 09:00:00 2026     clickhouse-watchdog  ")
    assert engines.owned_process(path, "--config-file=", engines.CLICKHOUSE_ALIASES) is None


def test_unrelated_process_with_the_same_start_time_is_not_claimed(record):
    path, set_live = record
    set_live("Fri Sep 11 04:09:48 2026     /usr/bin/python3 unrelated.py")
    assert engines.owned_process(path, "--config-file=", engines.CLICKHOUSE_ALIASES) is None


def test_exited_process_and_missing_record_report_no_owner(record, tmp_path):
    path, set_live = record
    set_live("")
    assert engines.owned_process(path, "--config-file=", engines.CLICKHOUSE_ALIASES) is None
    assert engines.owned_process(tmp_path / "absent.json", "x") is None


def test_stop_process_clears_a_stale_record_without_signalling(record, monkeypatch):
    path, set_live = record
    set_live("")
    monkeypatch.setattr(engines.os, "kill", lambda *args: pytest.fail("must not signal"))
    assert engines.stop_process(path, "--config-file=", aliases=engines.CLICKHOUSE_ALIASES) is False
    assert not path.exists()


# ----- engine registry --------------------------------------------------------------
def test_every_engine_has_a_starter_seeder_label_and_port():
    assert set(engines.ENGINES) == set(engines.STARTERS) == set(engines.SEEDERS)
    assert set(engines.ENGINES) >= set(docker_engines.DOCKER_ENGINES)
    for engine in engines.ENGINES:
        assert engines.LABELS[engine]
        assert engine in engines.PORTS


def test_docker_engine_ports_are_distinct_and_loopback_only():
    ports = [port for port in engines.PORTS.values() if port]
    assert len(ports) == len(set(ports))
    assert set(docker_engines.PORTS) == set(docker_engines.HTTP_PORTS) == set(docker_engines.DOCKER_ENGINES)
    assert not set(docker_engines.PORTS.values()) & set(docker_engines.HTTP_PORTS.values())


# ----- pinned images ----------------------------------------------------------------
def test_pinned_images_are_official_and_tagged():
    images = docker_engines.pins()["images"]
    assert set(images) == {"starrocks", "doris-fe", "doris-be", "hive"}
    for entry in images.values():
        assert ":" in entry["image"] and "latest" not in entry["image"].split(":")[1].split("-")[0]
        assert entry["license"] == "Apache-2.0"
        assert entry["digest"] is None or entry["digest"].startswith("sha256:")


def test_container_names_are_project_scoped():
    names = [name for group in docker_engines.CONTAINERS.values() for name in group]
    assert len(names) == len(set(names))
    assert all(name.startswith("lattice-") for name in names)
    assert all(name in docker_engines.MEMORY for name in names)


def test_foreign_container_with_our_name_is_refused(monkeypatch):
    monkeypatch.setattr(
        docker_engines, "docker",
        lambda *args, **kwargs: json.dumps({"status": "running", "label": "/somewhere/else", "image": "x"}),
    )
    with pytest.raises(RuntimeError, match="本项目之外"):
        docker_engines.container("lattice-hive")


def test_missing_container_is_reported_as_absent(monkeypatch):
    def fake(*args, **kwargs):
        raise RuntimeError("Error: No such object: lattice-hive")

    monkeypatch.setattr(docker_engines, "docker", fake)
    assert docker_engines.container("lattice-hive") is None


def test_image_digest_mismatch_refuses_to_run(monkeypatch, tmp_path):
    monkeypatch.setattr(docker_engines, "pins", lambda: {
        "images": {"hive": {"image": "apache/hive:4.0.1", "digest": "sha256:" + "0" * 64}}
    })
    monkeypatch.setattr(docker_engines, "image_digest", lambda reference: "sha256:" + "1" * 64)
    with pytest.raises(RuntimeError, match="摘要"):
        docker_engines.ensure_image("hive", tmp_path / "log")


# ----- generated DDL ----------------------------------------------------------------
COLUMNS = [("order_id", "INTEGER"), ("price", "DECIMAL(16,2)"), ("name", "VARCHAR"), ("day", "DATE")]


def test_olap_ddl_uses_a_single_replica_and_bucket_for_the_demo():
    sql = docker_engines.olap_create_sql("t_lattice_orders", COLUMNS)
    assert "`lattice_demo`.`t_lattice_orders`" in sql
    assert "DUPLICATE KEY(`order_id`)" in sql
    assert '"replication_num" = "1"' in sql
    assert "DECIMAL(16,2)" in sql and "VARCHAR(255)" in sql


def test_hive_types_map_to_hive_spellings():
    assert docker_engines.hive_type("VARCHAR") == "STRING"
    assert docker_engines.hive_type("DECIMAL(16,2)") == "DECIMAL(16,2)"
    assert docker_engines.hive_type("TIMESTAMP") == "TIMESTAMP"
    with pytest.raises(KeyError):
        docker_engines.hive_type("STRUCT")


def test_hive_text_escaping_keeps_one_row_per_line():
    assert docker_engines._hive_text(None) == "\\N"
    assert docker_engines._hive_text("a\nb\x01c") == "a b c"
    assert docker_engines._hive_text("back\\slash") == "back\\\\slash"


def test_engine_labels_cover_docker_engines():
    for engine in docker_engines.DOCKER_ENGINES:
        assert engines.LABELS[engine] == docker_engines.LABELS[engine]


def test_ps_failure_keeps_the_ownership_record(record, monkeypatch, tmp_path):
    path, _ = record

    def broken(pid):
        raise RuntimeError("ps 检查进程失败")

    monkeypatch.setattr(engines, "process_identity", broken)
    monkeypatch.setattr(engines.os, "kill", lambda *args: pytest.fail("must not signal"))
    with pytest.raises(RuntimeError):
        engines.stop_process(path, "--config-file=", aliases=engines.CLICKHOUSE_ALIASES)
    assert path.exists(), "a transient ps failure must not orphan a running engine"


def test_process_identity_distinguishes_absent_from_unusable(monkeypatch):
    class Result:
        def __init__(self, code, out="", err=""):
            self.returncode, self.stdout, self.stderr = code, out, err

    monkeypatch.setattr(engines.subprocess, "run", lambda *a, **k: Result(1))
    assert engines.process_identity(4242) == ""
    monkeypatch.setattr(engines.subprocess, "run", lambda *a, **k: Result(127, err="ps: not found"))
    with pytest.raises(RuntimeError):
        engines.process_identity(4242)

    def explode(*args, **kwargs):
        raise OSError("fork failed")

    monkeypatch.setattr(engines.subprocess, "run", explode)
    with pytest.raises(RuntimeError):
        engines.process_identity(4242)


def test_every_docker_engine_declares_its_volumes():
    assert set(docker_engines.VOLUMES) == set(docker_engines.DOCKER_ENGINES)
    names = [name for group in docker_engines.VOLUMES.values() for name in group]
    assert len(names) == len(set(names))
    assert all(name.startswith("lattice-") for name in names)


def test_foreign_volume_is_never_mounted(monkeypatch):
    monkeypatch.setattr(docker_engines, "docker", lambda *args, **kwargs: "/somewhere/else")
    with pytest.raises(RuntimeError, match="不属于本项目"):
        docker_engines.ensure_volume("lattice-doris-be-storage")


def test_missing_volume_is_created_with_the_project_label(monkeypatch):
    created = []

    def fake(*args, **kwargs):
        if args[:2] == ("volume", "inspect"):
            raise RuntimeError("Error: No such volume: x")
        created.append(args)
        return ""

    monkeypatch.setattr(docker_engines, "docker", fake)
    docker_engines.ensure_volume("lattice-doris-be-storage")
    assert created == [("volume", "create", "--label", f"lattice.project={ROOT}", "lattice-doris-be-storage")]


def run_container_probe(monkeypatch, tmp_path, state, image="apache/doris:fe-2.1.10", args=()):
    actions = []

    def fake(*call, **kwargs):
        actions.append(call[0])
        return ""

    monkeypatch.setattr(docker_engines, "container", lambda name: state)
    monkeypatch.setattr(docker_engines, "docker", fake)
    outcome = docker_engines.run_container("lattice-doris-fe", image, list(args), tmp_path / "log")
    return outcome, actions


def test_container_from_a_different_pinned_image_is_replaced(monkeypatch, tmp_path):
    state = {"status": "running", "label": str(ROOT), "image": "apache/doris:fe-2.1.9", "config": "x"}
    outcome, actions = run_container_probe(monkeypatch, tmp_path, state)
    assert outcome == "recreated"
    assert actions == ["rm", "run"]


def test_container_with_changed_run_options_is_replaced(monkeypatch, tmp_path):
    """A container created before a port, mount or tmpfs change must not be reused."""
    fresh, _ = run_container_probe(monkeypatch, tmp_path, None, args=("-p", "1:1"))
    assert fresh == "created"
    # Reuse only happens when the fingerprint of (image, memory, args) still matches.
    import hashlib, json as js

    def fingerprint(args):
        return hashlib.sha256(
            js.dumps(["apache/doris:fe-2.1.10", docker_engines.MEMORY["lattice-doris-fe"],
                      docker_engines.RESTART_POLICY, list(args)],
                     sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]

    same = {"status": "running", "label": str(ROOT), "image": "apache/doris:fe-2.1.10",
            "config": fingerprint(("-p", "1:1"))}
    outcome, actions = run_container_probe(monkeypatch, tmp_path, same, args=("-p", "1:1"))
    assert outcome == "reused" and actions == []
    outcome, actions = run_container_probe(monkeypatch, tmp_path, same, args=("-p", "2:2"))
    assert outcome == "recreated" and actions == ["rm", "run"]


def test_containers_come_back_with_docker_after_a_restart(monkeypatch, tmp_path):
    """A stopped container is exactly what the WebUI reports as an offline data source."""
    _, actions = run_container_probe(monkeypatch, tmp_path, None)
    assert actions == ["run"]
    recorded = []
    monkeypatch.setattr(docker_engines, "container", lambda name: None)
    monkeypatch.setattr(docker_engines, "docker", lambda *call, **kwargs: recorded.extend(call) or "")
    docker_engines.run_container("lattice-hive", "apache/hive:4.0.1", [], tmp_path / "log")
    assert recorded[recorded.index("--restart") + 1] == "unless-stopped"


class FakePull:
    """Stand-in for the docker CLI writing pull progress to a pipe."""

    class Pipe:
        def __init__(self, lines):
            self.lines, self.closed = iter(lines), False

        def __iter__(self):
            return self.lines

        def close(self):
            self.closed = True

    def __init__(self, lines, code=0):
        self.stdout, self.returncode, self.killed = self.Pipe(lines), code, False

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.killed = True


def test_pull_reports_layer_progress_and_keeps_the_full_log(monkeypatch, tmp_path, capsys):
    lines = ["3.4-latest: Pulling from starrocks/allin1-ubuntu\n",
             "aaa: Pulling fs layer\n", "bbb: Pulling fs layer\n",
             "aaa: Downloading [====>     ]  1.2GB/4.4GB\n",
             "aaa: Pull complete\n", "bbb: Already exists\n",
             "Status: Downloaded newer image for starrocks/allin1-ubuntu:3.4-latest\n"]
    monkeypatch.setattr(docker_engines, "docker_binary", lambda: "/usr/bin/docker")
    monkeypatch.setattr(docker_engines.subprocess, "Popen", lambda *a, **k: FakePull(lines))
    log = tmp_path / "docker.log"
    docker_engines.pull_image("starrocks/allin1-ubuntu:3.4-latest", log)
    assert "2 层" in capsys.readouterr().out
    assert "Pull complete" in log.read_text(), "the untruncated docker output must stay in the log"


def test_failed_pull_is_reported_with_the_log_path(monkeypatch, tmp_path):
    monkeypatch.setattr(docker_engines, "docker_binary", lambda: "/usr/bin/docker")
    monkeypatch.setattr(docker_engines.subprocess, "Popen",
                        lambda *a, **k: FakePull(["error pulling image: no space left on device\n"], code=1))
    log = tmp_path / "docker.log"
    with pytest.raises(RuntimeError, match="no space left on device"):
        docker_engines.pull_image("apache/hive:4.0.1", log)


def test_memory_limits_are_parsed_the_way_docker_reads_them():
    assert docker_engines.memory_bytes("2g") == 2 * 1024 ** 3
    assert docker_engines.memory_bytes("2500m") == 2500 * 1024 ** 2
    assert set(docker_engines.MEMORY) == {name for group in docker_engines.CONTAINERS.values() for name in group}


def test_a_docker_vm_too_small_is_named_as_the_cause(monkeypatch):
    needed = sum(docker_engines.FOOTPRINT.values())
    monkeypatch.setattr(docker_engines, "docker_memory", lambda: needed)
    assert docker_engines.memory_advice() == ""
    monkeypatch.setattr(docker_engines, "docker_memory", lambda: needed // 2)
    assert "Docker Desktop" in docker_engines.memory_advice()
    # An unreadable "docker info" must not invent a diagnosis.
    monkeypatch.setattr(docker_engines, "docker_memory", lambda: 0)
    assert docker_engines.memory_advice() == ""


def test_the_default_docker_desktop_vm_is_not_warned_about(monkeypatch):
    """All three engines together were measured at 4.3 GB; 8 GB must stay silent."""
    assert sum(docker_engines.FOOTPRINT.values()) < 8 * docker_engines.GIB
    assert set(docker_engines.FOOTPRINT) == set(docker_engines.DOCKER_ENGINES)
    monkeypatch.setattr(docker_engines, "docker_memory", lambda: 8320868352)
    assert docker_engines.memory_advice() == ""


def test_container_killed_for_memory_says_so_instead_of_just_exited(monkeypatch):
    state = {"status": "exited", "label": str(ROOT), "image": "x", "oomkilled": "true"}
    monkeypatch.setattr(docker_engines, "container", lambda name: state)
    monkeypatch.setattr(docker_engines, "docker_memory", lambda: 1024 ** 3)
    with pytest.raises(RuntimeError, match="内存不足"):
        docker_engines.wait_until(lambda: True, timeout=1, what="x", containers=("lattice-hive",))
    state["oomkilled"] = "false"
    with pytest.raises(RuntimeError, match="已退出"):
        docker_engines.wait_until(lambda: True, timeout=1, what="x", containers=("lattice-hive",))


def test_locally_built_image_is_not_retagged(monkeypatch, tmp_path):
    monkeypatch.setattr(docker_engines, "pins", lambda: {
        "images": {"hive": {"image": "apache/hive:4.0.1", "digest": "sha256:" + "a" * 64}}
    })
    monkeypatch.setattr(docker_engines, "image_digest", lambda reference: None)
    monkeypatch.setattr(docker_engines, "image_exists", lambda reference: True)
    with pytest.raises(RuntimeError, match="本地构建"):
        docker_engines.ensure_image("hive", tmp_path / "log")


def test_unlabelled_volume_is_adopted_only_when_our_containers_use_it(monkeypatch):
    calls = {"users": ["lattice-starrocks"]}

    def fake(*args, **kwargs):
        if args[:2] == ("volume", "inspect"):
            return ""  # exists, but carries no label
        if args[0] == "ps":
            return "\n".join(calls["users"])
        raise AssertionError(args)

    monkeypatch.setattr(docker_engines, "docker", fake)
    monkeypatch.setattr(docker_engines, "container", lambda name: {"status": "running", "label": str(ROOT), "image": "x"})
    assert docker_engines.ensure_volume("lattice-starrocks-fe-meta") == "lattice-starrocks-fe-meta"

    calls["users"] = ["someone-elses-db"]
    with pytest.raises(RuntimeError, match="不属于本项目"):
        docker_engines.ensure_volume("lattice-starrocks-fe-meta")

    calls["users"] = []
    with pytest.raises(RuntimeError, match="不属于本项目"):
        docker_engines.ensure_volume("lattice-starrocks-fe-meta")


# ----- quality datasource -----------------------------------------------------------
def test_start_writes_the_config_the_webui_needs_to_render_the_card(tmp_path, monkeypatch):
    """The builtin ``quality-postgres`` card is only rendered when this file exists.

    Nothing used to write it, so a rebuilt ``.runtime`` silently dropped the
    PostgreSQL card while the other eight sources appeared.
    """
    sys.path.insert(0, str(ROOT))
    from webapi.datasources import DataSourceRegistry

    runtime = tmp_path / "engines"
    runtime.mkdir()
    monkeypatch.setattr(engines, "RUNTIME", runtime)
    # Drive the real ``start`` entry point: the bug was not a broken helper but a
    # helper the startup path never called.
    monkeypatch.setattr(engines, "ENGINES", ())
    monkeypatch.setattr(engines, "seed", lambda available: [])
    assert engines.start() == 0

    written = runtime / "quality-datasource.json"
    assert written.is_file(), "the startup path must provision the datasource config"
    assert written.stat().st_mode & 0o777 == 0o600, "engine configs stay private"

    registry = DataSourceRegistry(tmp_path / "web", tmp_path / "polaris", runtime)
    record = registry.builtin_records()["quality-postgres"]
    assert record["type"] == "postgresql"
    assert record["config"]["database"] == "blog_converter"
    # The business tables live in ``public``; ``lattice_quality`` holds only the
    # quality module's own metadata and is reached through webapi.quality_store.
    assert record["config"]["schema"] == "public"


def test_quality_datasource_follows_the_configured_dsn(monkeypatch):
    """One definition of the DSN: the card must not drift from the quality store."""
    monkeypatch.setenv("LATTICE_QUALITY_DSN", "postgresql+asyncpg://bob:p%40ss@db.example:6543/biz?sslmode=require")
    assert engines.quality_datasource_config() == {
        "type": "postgresql", "host": "db.example", "port": 6543,
        "user": "bob", "password": "p@ss", "database": "biz", "sslmode": "require",
    }


def test_an_unusable_dsn_warns_instead_of_writing_a_broken_card(tmp_path, monkeypatch):
    runtime = tmp_path / "engines"
    runtime.mkdir()
    monkeypatch.setattr(engines, "RUNTIME", runtime)
    monkeypatch.setenv("LATTICE_QUALITY_DSN", "mysql://user@host/db")
    engines.write_quality_datasource()
    assert not (runtime / "quality-datasource.json").exists()
