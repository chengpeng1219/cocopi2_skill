import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts.maixpy_skill import dashboard
from scripts.maixpy_skill.config import set_device
from scripts.maixpy_skill.ssh import CommandResult


ENV = {
    "ok": True,
    "platform": {"system": "Windows", "python_version": "3.11"},
    "tools": {
        "ssh": "C:/ssh.exe",
        "scp": "C:/scp.exe",
        "sshpass": None,
        "dns-sd": "C:/dns-sd.exe",
    },
    "mdns": {"zeroconf": False, "avahi-browse": False, "dns-sd": True},
}


class DashboardTests(unittest.TestCase):
    def test_render_text_without_device_suggests_setup(self) -> None:
        status = {
            "environment": ENV,
            "deep": True,
            "configured": False,
            "device": None,
            "state": "unconfigured",
            "summary": "尚未配置设备",
            "advice": "请把 MaixCAM 屏幕“设置 -> 设备信息”里的 IP 告诉我，我来连接。",
            "checked_at": "2026-01-01 00:00:00",
        }

        text = dashboard.render_text(status)

        self.assertIn("MaixPy 设备连接", text)
        self.assertIn("尚未配置设备", text)
        self.assertIn("IP 告诉我", text)
        self.assertNotIn("device set", text)
        self.assertNotIn("python3", text)
        self.assertNotIn("sipeed", text)

    def test_summarize_prefers_conversation_over_commands(self) -> None:
        unconfigured = {
            "configured": False,
            "deep": True,
        }
        state, summary, advice = dashboard.summarize(unconfigured)
        self.assertEqual(state, "unconfigured")
        self.assertIn("IP", advice)
        self.assertNotIn("python3", advice)

        connected = {
            "configured": True,
            "deep": True,
            "device": {"host": "192.168.0.64"},
            "dns": {"ok": True},
            "tcp": {"ok": True},
            "ssh": {"ok": True},
            "remote": {"HOSTNAME": "maixcam2-9c54"},
        }
        state, summary, advice = dashboard.summarize(connected)
        self.assertEqual(state, "connected")
        self.assertIn("已连接", summary)
        self.assertIn("功能", advice)

        unreachable = {
            "configured": True,
            "deep": True,
            "device": {"host": "192.168.0.64"},
            "dns": {"ok": True},
            "tcp": {"ok": False},
            "ssh": {"ok": False},
            "remote": None,
        }
        state, summary, advice = dashboard.summarize(unreachable)
        self.assertEqual(state, "unreachable")
        self.assertIn("最新 IP", advice)

    def test_auth_failure_advice_tells_user_how_to_fix_it(self) -> None:
        status = {
            "configured": True,
            "deep": True,
            "device": {"host": "192.168.0.64"},
            "dns": {"ok": True},
            "tcp": {"ok": True},
            "ssh": {"ok": False, "stderr": "Permission denied (publickey,password)"},
            "remote": None,
            "environment": {
                "auth_backend": "interactive",
                "tools": {"ssh": "ssh", "scp": "scp", "sshpass": None},
                "mdns": {},
            },
        }

        state, summary, advice = dashboard.summarize(status)

        self.assertEqual(state, "auth_failed")
        self.assertIn("auth", advice)
        self.assertIn("pip install paramiko", advice)

    def test_collect_without_device_returns_setup_steps(self) -> None:
        with TemporaryDirectory() as tmp:
            with patch("scripts.maixpy_skill.dashboard.doctor.check", return_value=ENV):
                with patch("scripts.maixpy_skill.dashboard.discover.scan_candidates", return_value=[]):
                    status = dashboard.collect(cwd=Path(tmp), deep=False)

        self.assertFalse(status["configured"])
        self.assertEqual(status["discovered"], [])
        self.assertEqual(status["state"], "unconfigured")
        self.assertIn("IP", status["advice"])
        self.assertTrue(any("device set" in step for step in status["next_steps"]))
        self.assertNotIn("password", json.dumps(status))

    def test_collect_reports_connected_device_info(self) -> None:
        connected = CommandResult(
            0,
            "__MAIXPY_OK__\nHOSTNAME=maixcam2-9a6f\nMODEL=MaixCAM2\n"
            "OS=Ubuntu 22.04 LTS\nARCH=aarch64\nIP=192.168.66.152\n"
            "__MAIXPY_END__\n",
            "",
        )

        with TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            set_device("192.168.66.152", "root", device_class="maixcam2", cwd=cwd)
            with patch("scripts.maixpy_skill.dashboard.doctor.check", return_value=ENV):
                with patch(
                    "scripts.maixpy_skill.dashboard._resolve",
                    return_value={"ok": True, "address": "192.168.66.152", "latency_ms": 1.0, "error": None},
                ):
                    with patch(
                        "scripts.maixpy_skill.dashboard._tcp_probe",
                        return_value={"ok": True, "port": 22, "latency_ms": 2.0, "error": None},
                    ):
                        with patch(
                            "scripts.maixpy_skill.dashboard._run_remote",
                            return_value=(connected, "paramiko"),
                        ):
                            status = dashboard.collect(cwd=cwd, deep=True)

        self.assertTrue(status["ssh"]["ok"])
        self.assertEqual(status["state"], "connected")
        self.assertEqual(status["auth_backend"], "paramiko")
        self.assertEqual(status["remote"]["HOSTNAME"], "maixcam2-9a6f")
        self.assertTrue(any("连接正常" in step for step in status["next_steps"]))
        self.assertNotIn("password", json.dumps(status))

    def test_parse_info_ignores_noise(self) -> None:
        info = dashboard._parse_info(
            "Warning: something\n__MAIXPY_OK__\nHOSTNAME=maixcam2-abcd\nARCH=aarch64\n__MAIXPY_END__\n"
        )
        self.assertEqual(info, {"HOSTNAME": "maixcam2-abcd", "ARCH": "aarch64"})

    def test_html_contains_status_and_next_steps(self) -> None:
        status = {
            "environment": ENV,
            "state": "unconfigured",
            "summary": "尚未配置设备",
            "advice": "请把设备 IP 告诉我，我来连接。",
            "configured": False,
            "device": None,
            "dns": None,
            "tcp": None,
            "ssh": None,
            "remote": None,
            "discovered": [],
            "next_steps": ["请先配置设备"],
            "checked_at": "2026-01-01 00:00:00",
        }

        page = dashboard._html(status)

        self.assertIn("MaixPy Skill", page)
        self.assertIn("请把设备 IP 告诉我", page)
        self.assertNotIn("下一步操作", page)
        self.assertIn("/api/check", page)
        self.assertNotIn("sipeed", page)


if __name__ == "__main__":
    unittest.main()
