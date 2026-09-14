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
"""Lifecycle safety tests. No running service or external network is touched."""

import importlib.util
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location("polaris_service", Path(__file__).with_name("service.py"))
service = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(service)


class LifecycleSafetyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def receipt(self, **state):
        path = self.root / "receipt.json"
        path.write_text(json.dumps(state))
        return path

    def test_reused_pid_does_not_authorize_stopping_process(self):
        path = self.receipt(pid=7, identity="old-start java /project/polaris.jar")
        with patch.object(service, "process_identity", return_value="new-start java /project/polaris.jar"):
            self.assertIsNone(service.owned_process(path, "/project/polaris.jar"))

    def test_matching_pid_without_expected_command_is_not_owned(self):
        identity = "same-start unrelated-service"
        path = self.receipt(pid=7, identity=identity)
        with patch.object(service, "process_identity", return_value=identity):
            self.assertIsNone(service.owned_process(path, "polaris.jar"))

    def test_actual_listener_is_detected_without_stopping_it(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            with self.assertRaisesRegex(RuntimeError, "already in use"):
                service.require_free_port(port)
            self.assertNotEqual(listener.fileno(), -1)

    def test_rollback_preserves_preexisting_dependencies(self):
        state = {"pid": 7, "identity": "old-start"}
        path = self.receipt(server=state, minio=None, database=None)
        with patch.object(service, "owned_server", side_effect=[state, None]), \
                patch.object(service, "stop_process") as stop_process, \
                patch.object(service, "stop_database") as stop_database:
            service.rollback(path)
            stop_process.assert_called_once_with(service.PID_FILE, str(service.DIST / "server" / "quarkus-run.jar"))
            stop_database.assert_not_called()
        self.assertFalse(path.exists())

    def test_rollback_preserves_replacement_server_and_dependencies(self):
        old, new = {"pid": 7, "identity": "old"}, {"pid": 7, "identity": "new"}
        path = self.receipt(server=old, minio=old, database=old)
        with patch.object(service, "owned_server", return_value=new), \
                patch.object(service, "stop_process") as stop_process, \
                patch.object(service, "stop_database") as stop_database:
            service.rollback(path)
            stop_process.assert_not_called()
            stop_database.assert_not_called()

    def test_rollback_stops_only_matching_new_dependencies(self):
        old, new = {"pid": 7, "identity": "old"}, {"pid": 7, "identity": "new"}
        path = self.receipt(server=None, minio=old, database=old)
        with patch.object(service, "owned_server", return_value=None), \
                patch.object(service, "owned_minio", return_value=new), \
                patch.object(service, "database_identity", return_value=old), \
                patch.object(service, "stop_process") as stop_process, \
                patch.object(service, "stop_database") as stop_database:
            service.rollback(path)
            stop_process.assert_not_called()
            stop_database.assert_called_once()

    def test_start_existing_service_writes_empty_receipt(self):
        path = self.root / "receipt.json"
        with patch.object(service, "owned_server", return_value={"pid": 7}), \
                patch.object(service, "verify", return_value={"status": "UP"}), \
                patch("builtins.print"):
            service.start(path)
        self.assertEqual(json.loads(path.read_text()), {"server": None, "minio": None, "database": None})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_failed_start_preserves_existing_dependencies(self):
        with patch.object(service, "owned_server", return_value=None), \
                patch.object(service, "require_free_port"), \
                patch.object(service, "ensure_distribution"), \
                patch.object(service, "prepare_config"), \
                patch.object(service, "database_running", return_value=True), \
                patch.object(service, "owned_minio", return_value={"pid": 7}), \
                patch.object(service, "ensure_database"), \
                patch.object(service, "ensure_minio", side_effect=RuntimeError("startup failure")), \
                patch.object(service, "stop_process") as stop_process, \
                patch.object(service, "stop_database") as stop_database:
            with self.assertRaisesRegex(RuntimeError, "startup failure"):
                service.start()
            self.assertEqual(stop_process.call_count, 1)
            stop_database.assert_not_called()

    def test_private_write_replaces_symlink_without_touching_target(self):
        target = self.root / "existing.txt"
        target.write_text("unchanged")
        path = self.root / "private.json"
        path.symlink_to(target)
        service.write_private(path, "private")
        self.assertEqual(target.read_text(), "unchanged")
        self.assertFalse(path.is_symlink())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_stale_postmaster_pid_never_signals_unrelated_process(self):
        process = subprocess.Popen(["/bin/sleep", "30"])
        def reap_child():
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
        self.addCleanup(reap_child)
        data = self.root / "postgres"
        data.mkdir()
        (data / "PG_VERSION").write_text("16")
        (data / "postmaster.pid").write_text(f"{process.pid}\n{data}\n1\n")
        with patch.object(service, "PGDATA", data), patch.object(service, "pg_bin", return_value=self.root):
            with self.assertRaisesRegex(RuntimeError, "does not match"):
                service.stop_database()
        self.assertIsNone(process.poll())
        process.terminate()
        process.wait(timeout=5)

    def test_ownership_record_write_failure_reaps_detached_child(self):
        process = Mock(pid=7)
        process.poll.return_value = None
        with patch.object(service.subprocess, "Popen", return_value=process), \
                patch.object(service, "process_identity", return_value="start /project/polaris.jar"), \
                patch.object(service, "write_private", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                service.launch_process(["java"], log_path=self.root / "server.log", pid_file=self.root / "pid.json")
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=15)

    def test_interrupted_ownership_record_write_reaps_detached_child(self):
        process = Mock(pid=7)
        process.poll.return_value = None
        with patch.object(service.subprocess, "Popen", return_value=process), \
                patch.object(service, "process_identity", return_value="start /project/polaris.jar"), \
                patch.object(service, "write_private", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                service.launch_process(["java"], log_path=self.root / "server.log", pid_file=self.root / "pid.json")
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=15)

    def test_linux_ownership_uses_proc_executable_instead_of_ps_comm(self):
        data = self.root / "postgres"
        data.mkdir()
        timestamp = 1789107935
        (data / "PG_VERSION").write_text("16")
        (data / "postmaster.pid").write_text(f"42\n{data}\n{timestamp}\n")
        executable = str(self.root / "bin" / "postgres")
        real_readlink = service.os.readlink
        def fake_run(command, **kwargs):
            if command[-1] == "comm=":
                self.fail("Linux must not use ps comm as an executable path")
            if command[-1] == "command=":
                return f"{executable} -D {data}"
            return service.datetime.fromtimestamp(timestamp).strftime("%a %b %d %H:%M:%S %Y")
        with patch.object(service, "PGDATA", data), \
                patch.object(service.platform, "system", return_value="Linux"), \
                patch.object(service.os, "readlink", side_effect=lambda path, **kwargs: executable if str(path) == "/proc/42/exe" else real_readlink(path, **kwargs)) as readlink, \
                patch.object(service, "pg_bin", return_value=self.root / "bin"), \
                patch.object(service, "process_identity", return_value="same-process"), \
                patch.object(service, "run", side_effect=fake_run):
            self.assertEqual(service.database_identity(), {"pid": 42, "identity": "same-process"})
            readlink.assert_any_call("/proc/42/exe")


if __name__ == "__main__":
    unittest.main()
