import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts.maixpy_skill import deploy
from scripts.maixpy_skill.config import Device
from scripts.maixpy_skill.ssh import CommandResult


class FakeSession:
    backend = "paramiko"
    instances = []
    alive = True

    def __init__(self, *args, **kwargs):
        self.commands = []
        self.files = {}
        self.closed = False
        FakeSession.instances.append(self)

    def run(self, command, *, timeout=None):
        self.commands.append(command)
        if "echo __MAIXPY_ONLINE__" in command:
            return CommandResult(0, "__MAIXPY_ONLINE__\n", "")
        if "daemon=" in command:
            return CommandResult(0, "daemon=stopped\nlauncher=stopped\n", "")
        if "kill -0" in command:
            return CommandResult(0, "alive\n" if FakeSession.alive else "dead\n", "")
        if "setsid -f" in command:
            return CommandResult(0, "", "")
        if ".pid" in command and command.startswith("cat "):
            return CommandResult(0, "4242\n", "")
        if command.startswith("cat "):
            return CommandResult(0, "", "")
        return CommandResult(0, "", "")

    def put(self, local_path, remote_path):
        self.files[remote_path] = local_path
        return CommandResult(0, "", "")

    def get(self, remote_path, local_path):
        Path(local_path).write_bytes(b"fake-jpeg")
        return CommandResult(0, "", "")

    def close(self):
        self.closed = True


class DeployTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeSession.instances = []
        FakeSession.alive = True

    def test_deploy_runs_once_captures_and_restores(self) -> None:
        device = Device(host="192.168.0.64", user="root", password="secret")
        with TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            app = cwd / "main.py"
            app.write_text("from maix import app\nwhile not app.need_exit():\n    pass\n", encoding="utf-8")
            with patch("scripts.maixpy_skill.deploy.RemoteSession", FakeSession):
                out_dir = deploy.deploy_file(
                    device,
                    app,
                    background=True,
                    capture=True,
                    restore=True,
                    wait=0,
                    capture_timeout=1,
                    cwd=cwd,
                )

            summary = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
            session = FakeSession.instances[-1]
            self.assertEqual(summary["backend"], "paramiko")
            self.assertEqual(summary["pid"], "4242")
            self.assertTrue(summary["running"])
            self.assertTrue(summary["restored"])
            self.assertTrue((out_dir / "frame.jpg").exists())
            self.assertEqual(summary["frame"], str(out_dir / "frame.jpg"))
            self.assertNotIn("capture_pid", summary)
            self.assertEqual(sum(1 for c in session.commands if "setsid -f" in c), 1)
            self.assertNotIn("secret", json.dumps(summary))

    def test_deploy_directory_uploads_archive(self) -> None:
        device = Device(host="192.168.0.64", user="root", password="secret")
        with TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            app = cwd / "appdir"
            app.mkdir()
            (app / "main.py").write_text("print('hello')\n", encoding="utf-8")
            (app / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")

            with patch("scripts.maixpy_skill.deploy.RemoteSession", FakeSession):
                out_dir = deploy.deploy_file(
                    device,
                    app,
                    background=True,
                    capture=False,
                    wait=0,
                    cwd=cwd,
                )

            summary = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
            session = FakeSession.instances[-1]
            self.assertTrue(summary["directory"])
            self.assertTrue(str(summary["entry"]).endswith("main.py"))
            self.assertTrue(any(path.endswith("/manifest.json") for path in session.files))
            self.assertTrue(any(path.endswith("/main.py") for path in session.files))

    def test_deploy_marks_dead_process_as_not_running(self) -> None:
        FakeSession.alive = False
        device = Device(host="192.168.0.64", user="root", password="secret")
        with TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            app = cwd / "main.py"
            app.write_text("print('hello')\n", encoding="utf-8")
            with patch("scripts.maixpy_skill.deploy.RemoteSession", FakeSession):
                out_dir = deploy.deploy_file(
                    device,
                    app,
                    background=True,
                    capture=False,
                    wait=0,
                    cwd=cwd,
                )

            summary = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
            self.assertFalse(summary["running"])

    def test_directory_sync_skips_unchanged_files(self) -> None:
        class SyncSession:
            def __init__(self, manifest):
                self.manifest = manifest
                self.puts = []

            def run(self, command, *, timeout=None):
                return CommandResult(0, "", "")

            def get(self, remote_path, local_path):
                Path(local_path).write_text(json.dumps(self.manifest), encoding="utf-8")
                return CommandResult(0, "", "")

            def put(self, local_path, remote_path):
                self.puts.append(remote_path)
                if remote_path.endswith("/manifest.json"):
                    self.manifest = json.loads(Path(local_path).read_text(encoding="utf-8"))
                return CommandResult(0, "", "")

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text("print('one')\n", encoding="utf-8")
            manifest = deploy._file_manifest(root)
            session = SyncSession({})

            stats = deploy._sync_directory(session, root, "/cache/app")
            self.assertEqual(stats["uploaded"], 1)
            self.assertIn("/cache/app/main.py", session.puts)
            self.assertIn("/cache/app/manifest.json", session.puts)

            (root / "main.py").write_text("print('two')\n", encoding="utf-8")
            session.puts.clear()
            stats = deploy._sync_directory(session, root, "/cache/app")
            self.assertEqual(stats["uploaded"], 1)
            self.assertIn("/cache/app/main.py", session.puts)
            self.assertIn("/cache/app/manifest.json", session.puts)

            session.puts.clear()
            stats = deploy._sync_directory(session, root, "/cache/app")
            self.assertEqual(stats["uploaded"], 0)
            self.assertEqual(stats["skipped"], 1)
            self.assertEqual(session.puts, ["/cache/app/manifest.json"])

    def test_connection_error_detection(self) -> None:
        self.assertTrue(deploy._looks_like_connection_error(RuntimeError("timed out opening channel")))
        self.assertFalse(deploy._looks_like_connection_error(RuntimeError("display init failed")))


if __name__ == "__main__":
    unittest.main()
