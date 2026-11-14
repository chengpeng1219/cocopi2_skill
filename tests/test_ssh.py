import unittest
from unittest.mock import patch

from scripts.maixpy_skill.config import Device
from scripts.maixpy_skill.ssh import command_env, scp_to_command, ssh_command


class SshCommandTests(unittest.TestCase):
    def test_password_uses_sshpass_env_when_available(self) -> None:
        device = Device(host="192.168.1.2", user="root", password="secret")

        with patch("scripts.maixpy_skill.ssh.shutil.which", return_value="/usr/bin/sshpass"):
            command = ssh_command(device, "echo ok")

        self.assertEqual(command[:2], ["sshpass", "-e"])
        self.assertIn("ssh", command)
        self.assertNotIn("secret", command)
        self.assertEqual(command_env(device)["SSHPASS"], "secret")

    def test_password_falls_back_to_terminal_prompt_without_sshpass(self) -> None:
        device = Device(host="192.168.1.2", user="root", password="secret")

        with patch("scripts.maixpy_skill.ssh.shutil.which", return_value=None):
            command = scp_to_command(device, "main.py", "/tmp/main.py")

        self.assertEqual(command[0], "scp")
        self.assertNotIn("sshpass", command)
        self.assertNotIn("secret", command)

    def test_batch_mode_is_added_for_non_interactive_probes(self) -> None:
        device = Device(host="192.168.1.2", user="root")

        command = ssh_command(device, "echo ok", batch_mode=True)

        self.assertIn("BatchMode=yes", command)


if __name__ == "__main__":
    unittest.main()
