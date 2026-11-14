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

    def __init__(self, *args, **kwargs):
        self.commands = []
        self.files = {}
        self.closed = False

    def run(self, command, *, timeout=None):
        self.commands.append(command)
        if "echo __MAIXPY_ONLINE__" in command:
            return CommandResult(0, "__MAIXPY_ONLINE__\n", "")
        if "daemon=" in command:
            return CommandResult(0, "daemon=stopped\nlauncher=stopped\n", "")
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
    def test_deploy_runs_captures_and_restores(self) -> None:
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
                    cwd=cwd,
                )

            summary = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["backend"], "paramiko")
            self.assertEqual(summary["pid"], "4242")
            self.assertEqual(summary["capture_pid"], "4242")
            self.assertTrue(summary["restored"])
            self.assertTrue((out_dir / "frame.jpg").exists())
            self.assertEqual(summary["frame"], str(out_dir / "frame.jpg"))
            self.assertNotIn("secret", json.dumps(summary))

    def test_connection_error_detection(self) -> None:
        self.assertTrue(deploy._looks_like_connection_error(RuntimeError("timed out opening channel")))
        self.assertFalse(deploy._looks_like_connection_error(RuntimeError("display init failed")))


if __name__ == "__main__":
    unittest.main()
