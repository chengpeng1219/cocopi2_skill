import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts.maixpy_skill import cli
from scripts.maixpy_skill.config import ConfigError, Device
from scripts.maixpy_skill.discover import DiscoveredDevice
from scripts.maixpy_skill.ssh import CommandResult


class CliTests(unittest.TestCase):
    def test_device_check_without_config_prints_setup_prompt(self) -> None:
        devices = [
            DiscoveredDevice(host="maixcam2-4ea6.local", name="maixcam2-4ea6", device_class="maixcam2")
        ]

        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch("scripts.maixpy_skill.cli.get_device", side_effect=ConfigError("no current device configured")):
            with patch("scripts.maixpy_skill.cli.discover.scan_candidates", return_value=devices):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = cli.main(["device", "check"])

        self.assertEqual(code, 2)
        self.assertIn("ERROR: no current device configured", stderr.getvalue())
        self.assertIn("maixcam2-4ea6", stdout.getvalue())
        self.assertIn("IP 告诉我", stdout.getvalue())
        self.assertNotIn("请输入你的密码", stdout.getvalue())
        self.assertNotIn("账户名", stdout.getvalue())

    def test_device_check_success_prompts_for_feature_request(self) -> None:
        stdout = io.StringIO()
        device = Device(host="maixcam2-4ea6.local", user="root", device_class="maixcam2")
        with patch("scripts.maixpy_skill.cli.get_device", return_value=device):
            with patch(
                "scripts.maixpy_skill.cli.run_ssh",
                return_value=CommandResult(0, "connected\nLinux maixcam\n", ""),
            ):
                with redirect_stdout(stdout):
                    code = cli.main(["device", "check"])

        self.assertEqual(code, 0)
        self.assertIn("connected", stdout.getvalue())
        self.assertIn("配置完成, 请描述你的功能", stdout.getvalue())

    def test_auth_prompts_saves_and_verifies_without_echoing_password(self) -> None:
        stdout = io.StringIO()
        fake_session = type(
            "FakeSession",
            (),
            {
                "run": lambda self, command, timeout=None: CommandResult(0, "__AUTH_OK__\n", ""),
                "close": lambda self: None,
            },
        )()
        config = {
            "current_device": "dev",
            "devices": {"dev": {"host": "192.168.1.2", "user": "root", "device_class": "maixcam2"}},
        }
        device = Device(host="192.168.1.2", user="root", device_class="maixcam2", password="top-secret")

        with patch("scripts.maixpy_skill.cli.load_config", return_value=config):
            with patch("scripts.maixpy_skill.cli.getpass.getpass", return_value="top-secret"):
                with patch("scripts.maixpy_skill.cli.sys.stdin.isatty", return_value=True):
                    with patch("scripts.maixpy_skill.cli.set_device"):
                        with patch("scripts.maixpy_skill.cli.save_device_password") as save:
                            with patch("scripts.maixpy_skill.cli.paramiko_available", return_value=True):
                                with patch("scripts.maixpy_skill.cli.get_device", return_value=device):
                                    with patch("scripts.maixpy_skill.cli.RemoteSession", return_value=fake_session):
                                        with redirect_stdout(stdout):
                                            code = cli.main(["auth"])

        self.assertEqual(code, 0)
        self.assertIn("认证成功", stdout.getvalue())
        self.assertNotIn("top-secret", stdout.getvalue())
        save.assert_called_once()

    def test_auth_auto_installs_paramiko_without_confirmation(self) -> None:
        stdout = io.StringIO()
        fake_session = type(
            "FakeSession",
            (),
            {
                "run": lambda self, command, timeout=None: CommandResult(0, "__AUTH_OK__\n", ""),
                "close": lambda self: None,
            },
        )()
        config = {
            "current_device": "dev",
            "devices": {"dev": {"host": "192.168.1.2", "user": "root", "device_class": "maixcam2"}},
        }
        device = Device(host="192.168.1.2", user="root", device_class="maixcam2", password="top-secret")

        with patch("scripts.maixpy_skill.cli.load_config", return_value=config):
            with patch("scripts.maixpy_skill.cli.getpass.getpass", return_value="top-secret"):
                with patch("scripts.maixpy_skill.cli.sys.stdin.isatty", return_value=True):
                    with patch("scripts.maixpy_skill.cli.set_device"):
                        with patch("scripts.maixpy_skill.cli.save_device_password"):
                            with patch("scripts.maixpy_skill.cli.paramiko_available", return_value=False):
                                with patch("scripts.maixpy_skill.cli.shutil.which", return_value=None):
                                    with patch("scripts.maixpy_skill.cli.install_paramiko", return_value=True) as install:
                                        with patch("scripts.maixpy_skill.cli.get_device", return_value=device):
                                            with patch("scripts.maixpy_skill.cli.RemoteSession", return_value=fake_session):
                                                with redirect_stdout(stdout):
                                                    code = cli.main(["auth"])

        self.assertEqual(code, 0)
        install.assert_called_once()
        self.assertIn("自动安装 paramiko", stdout.getvalue())
        self.assertIn("认证成功", stdout.getvalue())

    def test_install_paramiko_uses_current_python(self) -> None:
        completed = type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        with patch("scripts.maixpy_skill.cli.subprocess.run", return_value=completed) as run:
            ok = cli.install_paramiko()

        self.assertTrue(ok)
        command = run.call_args.args[0]
        self.assertEqual(command[:3], [cli.sys.executable, "-m", "pip"])
        self.assertIn("paramiko", command)

    def test_install_paramiko_failure_reports_manual_fallback(self) -> None:
        stdout = io.StringIO()
        completed = type("Completed", (), {"returncode": 1, "stdout": "", "stderr": "network unavailable"})()
        with patch("scripts.maixpy_skill.cli.subprocess.run", return_value=completed):
            with redirect_stdout(stdout):
                ok = cli.install_paramiko()

        self.assertFalse(ok)
        self.assertIn("自动安装 paramiko 失败", stdout.getvalue())
        self.assertIn("network unavailable", stdout.getvalue())

    def test_auth_non_interactive_refuses_cleanly(self) -> None:
        stdout = io.StringIO()
        with patch("scripts.maixpy_skill.cli.sys.stdin.isatty", return_value=False):
            with redirect_stdout(stdout):
                code = cli.main(["auth"])
        self.assertEqual(code, 2)
        self.assertIn("不是交互式终端", stdout.getvalue())

    def test_auth_failed_login_does_not_overwrite_password(self) -> None:
        stdout = io.StringIO()
        fake_session = type(
            "FakeSession",
            (),
            {
                "run": lambda self, command, timeout=None: CommandResult(
                    1, "", "Permission denied (publickey,password)"
                ),
                "close": lambda self: None,
            },
        )()
        config = {
            "current_device": "dev",
            "devices": {"dev": {"host": "192.168.1.2", "user": "root", "device_class": "maixcam2"}},
        }

        with patch("scripts.maixpy_skill.cli.load_config", return_value=config):
            with patch("scripts.maixpy_skill.cli.getpass.getpass", return_value="wrong"):
                with patch("scripts.maixpy_skill.cli.sys.stdin.isatty", return_value=True):
                    with patch("scripts.maixpy_skill.cli.paramiko_available", return_value=True):
                        with patch("scripts.maixpy_skill.cli.save_device_password") as save:
                            with patch("scripts.maixpy_skill.cli.RemoteSession", return_value=fake_session):
                                with redirect_stdout(stdout):
                                    code = cli.main(["auth"])

        self.assertEqual(code, 1)
        save.assert_not_called()
        self.assertIn("密码未保存", stdout.getvalue())

    def test_auth_host_switch_tests_the_new_host(self) -> None:
        fake_session = type(
            "FakeSession",
            (),
            {
                "run": lambda self, command, timeout=None: CommandResult(0, "__AUTH_OK__\n", ""),
                "close": lambda self: None,
            },
        )()
        config = {
            "current_device": "dev",
            "devices": {"dev": {"host": "192.168.1.2", "user": "root", "device_class": "maixcam2"}},
        }

        with patch("scripts.maixpy_skill.cli.load_config", return_value=config):
            with patch("scripts.maixpy_skill.cli.getpass.getpass", return_value="pw"):
                with patch("scripts.maixpy_skill.cli.sys.stdin.isatty", return_value=True):
                    with patch("scripts.maixpy_skill.cli.paramiko_available", return_value=True):
                        with patch("scripts.maixpy_skill.cli.set_device") as set_device:
                            with patch("scripts.maixpy_skill.cli.save_device_password"):
                                with patch("scripts.maixpy_skill.cli.RemoteSession", return_value=fake_session) as session_cls:
                                    code = cli.main(["auth", "--host", "10.0.0.2"])

        self.assertEqual(code, 0)
        self.assertEqual(session_cls.call_args.args[0].host, "10.0.0.2")
        set_device.assert_called_once()

    def test_serve_refuses_lan_without_explicit_flag(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = cli.main(["serve", "--host", "0.0.0.0"])
        self.assertEqual(code, 2)
        self.assertIn("--allow-lan", stdout.getvalue())

    def test_deploy_returns_nonzero_when_capture_missing(self) -> None:
        with TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            (out_dir / "summary.json").write_text(
                json.dumps({"frame": None, "running": True}), encoding="utf-8"
            )
            with patch("scripts.maixpy_skill.cli.get_device", return_value=Device(host="x", user="root")):
                with patch("scripts.maixpy_skill.cli.deploy.deploy_file", return_value=out_dir):
                    with redirect_stdout(io.StringIO()):
                        code = cli.main(["deploy", "app.py"])
        self.assertEqual(code, 1)

    def test_deploy_returns_nonzero_when_background_process_dead(self) -> None:
        with TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            (out_dir / "summary.json").write_text(
                json.dumps({"frame": "frame.jpg", "running": False}), encoding="utf-8"
            )
            with patch("scripts.maixpy_skill.cli.get_device", return_value=Device(host="x", user="root")):
                with patch("scripts.maixpy_skill.cli.deploy.deploy_file", return_value=out_dir):
                    with redirect_stdout(io.StringIO()):
                        code = cli.main(["deploy", "app.py"])
        self.assertEqual(code, 1)

    def test_connect_detects_device_class_and_saves_it(self) -> None:
        fake_session = type(
            "FakeSession",
            (),
            {
                "run": lambda self, command, timeout=None: CommandResult(
                    0,
                    "__OK__\nHOSTNAME=maixcam2-9c54\nMODEL=MaixCAM2\nARCH=aarch64\n",
                    "",
                ),
                "close": lambda self: None,
            },
        )()
        config = {"current_device": None, "devices": {}}
        with patch("scripts.maixpy_skill.cli.load_config", return_value=config):
            with patch("scripts.maixpy_skill.cli.device_password", return_value="pw"):
                with patch("scripts.maixpy_skill.cli.paramiko_available", return_value=True):
                    with patch("scripts.maixpy_skill.cli.set_device") as set_device:
                        with patch("scripts.maixpy_skill.cli.RemoteSession", return_value=fake_session):
                            with redirect_stdout(io.StringIO()):
                                code = cli.main(["connect", "--host", "192.168.0.64"])

        self.assertEqual(code, 0)
        set_device.assert_called_once()
        self.assertEqual(set_device.call_args.args[3], "maixcam2")

    def test_connect_auth_failure_points_to_auth_command(self) -> None:
        stdout = io.StringIO()
        fake_session = type(
            "FakeSession",
            (),
            {
                "run": lambda self, command, timeout=None: CommandResult(
                    255, "", "Permission denied (publickey,password)"
                ),
                "close": lambda self: None,
            },
        )()
        config = {"current_device": None, "devices": {}}
        with patch("scripts.maixpy_skill.cli.load_config", return_value=config):
            with patch("scripts.maixpy_skill.cli.device_password", return_value="pw"):
                with patch("scripts.maixpy_skill.cli.paramiko_available", return_value=True):
                    with patch("scripts.maixpy_skill.cli.RemoteSession", return_value=fake_session):
                        with redirect_stdout(stdout):
                            code = cli.main(["connect", "--host", "192.168.0.64"])

        self.assertEqual(code, 1)
        self.assertIn("auth", stdout.getvalue())

    def test_repair_reports_single_daemon(self) -> None:
        class RepairSession:
            def __init__(self):
                self.calls = 0

            def run(self, command, *, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    return CommandResult(0, "daemon=running\n", "")
                return CommandResult(0, "daemon_count=1\ndaemon=running\nlauncher=running\n", "")

            def close(self):
                pass

        with patch("scripts.maixpy_skill.cli.get_device", return_value=Device(host="x", user="root")):
            with patch("scripts.maixpy_skill.cli.RemoteSession", return_value=RepairSession()):
                with redirect_stdout(io.StringIO()):
                    code = cli.main(["repair"])
        self.assertEqual(code, 0)

    def test_runs_lists_recent_summary(self) -> None:
        with TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            run_dir = cwd / ".maixpy" / "runs" / "run1"
            run_dir.mkdir(parents=True)
            (run_dir / "summary.json").write_text(
                json.dumps({"frame": "frame.jpg", "running": True}), encoding="utf-8"
            )
            stdout = io.StringIO()
            with patch("scripts.maixpy_skill.cli.Path.cwd", return_value=cwd):
                with redirect_stdout(stdout):
                    code = cli.main(["runs"])

        self.assertEqual(code, 0)
        self.assertIn("run1", stdout.getvalue())
        self.assertIn("running", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
