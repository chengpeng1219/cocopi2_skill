import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
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


if __name__ == "__main__":
    unittest.main()
