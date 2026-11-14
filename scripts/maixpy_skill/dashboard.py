"""Connection dashboard: local environment, device config, reachability, SSH, and device info."""
from __future__ import annotations

import json
import shutil
import socket
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import discover, doctor
from .config import ConfigError, get_device, load_config
from .ssh import CommandResult, run_ssh


REMOTE_INFO_COMMAND = r"""
echo __MAIXPY_OK__
echo "HOSTNAME=$(hostname 2>/dev/null)"
echo "KERNEL=$(uname -r 2>/dev/null)"
echo "ARCH=$(uname -m 2>/dev/null)"
echo "MODEL=$(tr -d '\000' < /proc/device-tree/model 2>/dev/null)"
echo "OS=$(grep '^PRETTY_NAME=' /etc/os-release 2>/dev/null | cut -d= -f2- | tr -d '\"')"
echo "UPTIME=$(cut -d. -f1 /proc/uptime 2>/dev/null)"
echo "IP=$(hostname -I 2>/dev/null | tr ' ' ',' | sed 's/,$//')"
echo "PYTHON=$(python3 --version 2>&1 || python --version 2>&1)"
echo "LAUNCHER=$(ps x 2>/dev/null | grep -F '/maixapp/apps/launcher/launcher daemon' | grep -v grep >/dev/null && echo running || echo stopped)"
echo "APPS=$(ls /maixapp/apps 2>/dev/null | wc -l)"
echo __MAIXPY_END__
""".strip()


def _tool_guidance(environment: dict[str, Any], auth_backend: str | None = None) -> list[str]:
    steps: list[str] = []
    tools = environment.get("tools") or {}
    if not tools.get("ssh") or not tools.get("scp"):
        steps.append(
            "本机缺少 SSH/SCP 客户端。Windows 可在“设置 -> 应用 -> 可选功能”安装 OpenSSH 客户端；"
            "Linux 安装 openssh-client。skill 本身不需要额外的 Python 插件。"
        )
    if not tools.get("sshpass") and auth_backend != "paramiko":
        steps.append(
            "未安装 sshpass（可选）。自动填写密码不可用，请在终端手动输入密码，"
            "或安装 sshpass 后把密码写入 .maixpy/device_credentials.local.json。"
        )
    mdns = environment.get("mdns") or {}
    if not any(mdns.values()):
        steps.append(
            "本机没有可用的 mDNS 发现工具（可选）。这不影响连接："
            "直接在设备“设置 -> 设备信息”里查看 IP，然后用 device set 填写 IP。"
        )
    return steps


def _resolve(host: str) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        address = socket.gethostbyname(host)
        return {
            "ok": True,
            "address": address,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "error": None,
        }
    except OSError as exc:
        return {"ok": False, "address": None, "latency_ms": None, "error": str(exc)}


def _tcp_probe(host: str, port: int = 22, timeout: float = 3.0) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
        return {
            "ok": True,
            "port": port,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "error": None,
        }
    except OSError as exc:
        return {"ok": False, "port": port, "latency_ms": None, "error": str(exc)}


def _parse_info(stdout: str) -> dict[str, str]:
    info: dict[str, str] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line or "=" not in line or line.startswith("__"):
            continue
        key, value = line.split("=", 1)
        if key.isupper():
            info[key] = value.strip()
    return info


def _run_remote(device, command: str, timeout: int) -> tuple[CommandResult, str]:
    """Run a remote command, preferring sshpass, then optional paramiko, then plain ssh."""
    can_sshpass = bool(device.password and shutil.which("sshpass"))
    if not can_sshpass and device.password:
        try:
            import paramiko
        except ImportError:
            paramiko = None  # type: ignore[assignment]
        if paramiko is not None:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            try:
                client.connect(
                    device.host,
                    username=device.user,
                    password=device.password,
                    timeout=timeout,
                    look_for_keys=False,
                    allow_agent=False,
                )
                _, stdout, stderr = client.exec_command(command, timeout=timeout)
                out = stdout.read().decode(errors="replace")
                err = stderr.read().decode(errors="replace")
                code = stdout.channel.recv_exit_status()
                return CommandResult(code, out, err), "paramiko"
            finally:
                client.close()
    result = run_ssh(device, command, timeout=timeout, batch_mode=not can_sshpass)
    return result, ("sshpass" if can_sshpass else "ssh")


def _next_steps(status: dict[str, Any], python_cmd: str) -> list[str]:
    steps: list[str] = []
    configured = status.get("configured")
    dns = status.get("dns") or {}
    tcp = status.get("tcp") or {}
    ssh = status.get("ssh") or {}

    steps.extend(_tool_guidance(status.get("environment") or {}, status.get("auth_backend")))

    if not configured:
        steps.append("尚未配置设备。先确认电脑与设备在同一 WiFi。")
        discovered = status.get("discovered") or []
        if discovered:
            steps.append("已发现以下设备，请确认后保存其中一个：")
            for item in discovered:
                steps.append(
                    f"  {item['name']}  host={item['host']}  type={item['device_class']}"
                )
        else:
            steps.append("未扫描到设备；请在设备“设置 -> 设备信息”查看 IP。")
        steps.append(
            f"保存设备：python3 {python_cmd} device set --host <IP> --user root "
            f"--device-class <maixcam-pro|maixcam2>"
        )
        steps.append("密码只放在 .maixpy/device_credentials.local.json，或在本终端输入，不要发到对话里。")
        return steps

    if dns.get("ok") is False:
        steps.append("主机名无法解析。请把 --host 改成设备 IP，并确认与设备在同一网段。")
    if tcp.get("ok") is False:
        steps.append("22 端口连不上。检查 IP 是否正确、设备是否在线、路由器是否开启 AP 隔离、电脑防火墙是否放行。")
    if ssh.get("ok") is False and tcp.get("ok") is not False:
        if "batch mode" in (ssh.get("stderr") or "").lower() or ssh.get("returncode") in (255, None):
            steps.append(
                "SSH 认证未完成。把密码写入 .maixpy/device_credentials.local.json，"
                "或在本终端运行 device check 并按提示输入密码。"
            )
    if ssh.get("ok"):
        steps.append("连接正常。可以直接描述要开发的功能，或运行 run <file.py>。")
    return steps


def summarize(status: dict[str, Any]) -> tuple[str, str, str]:
    """Return (state, one-line summary, one-line advice) for the user."""
    if not status.get("configured"):
        return (
            "unconfigured",
            "尚未配置设备",
            "请把 MaixCAM 屏幕“设置 -> 设备信息”里的 IP 告诉我，我来连接。",
        )
    if not status.get("deep"):
        return (
            "unknown",
            "尚未检测连接",
            "运行不带 --no-deep 的 status 即可检测；也可以直接告诉我 IP。",
        )

    device = status.get("device") or {}
    remote = status.get("remote") or {}
    dns = status.get("dns") or {}
    tcp = status.get("tcp") or {}
    ssh = status.get("ssh") or {}

    if ssh.get("ok"):
        name = remote.get("HOSTNAME") or device.get("host", "设备")
        return (
            "connected",
            f"已连接：{name}（{device.get('host', '-')}）",
            "可以直接告诉我要开发的功能。",
        )
    if tcp.get("ok") is False:
        return (
            "unreachable",
            "未连接：设备网络不可达",
            "请确认设备和电脑在同一 WiFi，然后把最新 IP 告诉我。",
        )
    if ssh.get("ok") is False:
        return (
            "auth_failed",
            "网络可达，但 SSH 登录未完成",
            "我会使用已保存的凭据重试；如果仍失败，再请你在本机终端按提示输入密码。",
        )
    if dns.get("ok") is False:
        return (
            "unreachable",
            "未连接：主机名无法解析",
            "请直接告诉我设备 IP，我用 IP 连接。",
        )
    return (
        "unknown",
        "连接状态未知",
        "我会重新检测一次。",
    )


def _finalize(status: dict[str, Any]) -> dict[str, Any]:
    state, summary, advice = summarize(status)
    status["state"] = state
    status["summary"] = summary
    status["advice"] = advice
    return status


def collect(*, cwd: Path | None = None, deep: bool = True, timeout: int = 8) -> dict[str, Any]:
    """Collect a full, secret-free connection status snapshot."""
    python_cmd = Path(__import__("sys").argv[0]).as_posix()
    status: dict[str, Any] = {
        "environment": doctor.check(),
        "deep": deep,
        "configured": False,
        "device": None,
        "dns": None,
        "tcp": None,
        "ssh": None,
        "remote": None,
        "discovered": [],
        "next_steps": [],
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    config = load_config(cwd)
    if not config.get("current_device"):
        if deep:
            try:
                status["discovered"] = [
                    {
                        "name": item.name,
                        "host": item.host,
                        "device_class": item.device_class,
                    }
                    for item in discover.scan_candidates(timeout=2)
                ]
            except Exception:
                status["discovered"] = []
        status["next_steps"] = _next_steps(status, python_cmd)
        return _finalize(status)

    status["configured"] = True
    try:
        device = get_device(cwd=cwd)
    except ConfigError as exc:
        status["config_error"] = str(exc)
        status["next_steps"] = _next_steps(status, python_cmd)
        return _finalize(status)

    status["device"] = device.public_dict()
    if not deep:
        status["next_steps"] = _next_steps(status, python_cmd)
        return _finalize(status)

    status["dns"] = _resolve(device.host)
    probe_host = (status["dns"] or {}).get("address") or device.host
    status["tcp"] = _tcp_probe(probe_host, 22)

    try:
        result, backend = _run_remote(device, REMOTE_INFO_COMMAND, timeout)
        status["auth_backend"] = backend
        status["ssh"] = {
            "ok": result.returncode == 0 and "__MAIXPY_OK__" in result.stdout,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
        if status["ssh"]["ok"]:
            status["remote"] = _parse_info(result.stdout)
    except Exception as exc:
        status["ssh"] = {"ok": False, "returncode": None, "stdout": "", "stderr": str(exc)}

    status["next_steps"] = _next_steps(status, python_cmd)
    return _finalize(status)


def _pill(ok: Any) -> str:
    if ok is True:
        return "状态：已连接"
    if ok is False:
        return "状态：未连接"
    return "状态：未检测"


def render_text(status: dict[str, Any], verbose: bool = False) -> str:
    """Render a short status by default, with optional technical detail."""
    if not verbose:
        device = status.get("device") or {}
        remote = status.get("remote") or {}
        lines = [
            "MaixPy 设备连接",
            "=" * 32,
            f"状态: {status.get('summary', '连接状态未知')}",
            f"建议: {status.get('advice', '')}",
        ]
        if device:
            lines.append("")
            lines.append(f"设备: {device.get('name', '-')} ({device.get('device_class', '-')})")
            lines.append(f"地址: {device.get('host', '-')}")
            if remote:
                lines.append(f"系统: {remote.get('OS', '-')} / {remote.get('ARCH', '-')}")
                lines.append(f"Python: {remote.get('PYTHON', '-')}")
                lines.append(f"应用数: {remote.get('APPS', '-')}")
        lines.append("")
        lines.append(f"检测时间: {status.get('checked_at', '-')}")
        return "\n".join(lines)

    env = status.get("environment") or {}
    lines: list[str] = []
    lines.append("=" * 64)
    lines.append(" MaixPy Skill - 设备连接面板")
    lines.append("=" * 64)
    lines.append(f"状态: {status.get('summary', '连接状态未知')}")
    lines.append(f"建议: {status.get('advice', '')}")
    lines.append("")
    lines.append(f"检测时间: {status.get('checked_at', '-')}")
    lines.append("")

    platform_info = env.get("platform") or {}
    tools = env.get("tools") or {}
    mdns = env.get("mdns") or {}
    lines.append("[本机环境]")
    lines.append(f"  系统: {platform_info.get('system', '-')}  Python: {platform_info.get('python_version', '-')}")
    lines.append(f"  ssh: {'可用' if tools.get('ssh') else '缺失'}    scp: {'可用' if tools.get('scp') else '缺失'}")
    lines.append(f"  sshpass: {'可用' if tools.get('sshpass') else '缺失（可选）'}")
    lines.append(f"  mDNS: {'可用' if any(mdns.values()) else '不可用（可选）'}")
    lines.append("")

    device = status.get("device")
    lines.append("[当前设备配置]")
    if device:
        lines.append(f"  名称: {device.get('name')}    类型: {device.get('device_class')}")
        lines.append(f"  地址: {device.get('host')}    用户: {device.get('user')}")
    else:
        lines.append("  尚未配置设备")
    lines.append("")

    if status.get("configured"):
        dns = status.get("dns") or {}
        tcp = status.get("tcp") or {}
        ssh = status.get("ssh") or {}
        deep = status.get("deep", True)
        lines.append("[连接检查]")
        if not deep:
            lines.append("  已跳过网络与 SSH 检测（--no-deep）")
        else:
            def _check(value: Any, yes: str, no: str) -> str:
                if value is True:
                    return yes
                if value is False:
                    return no
                return "未检测"

            lines.append(f"  DNS: {_check(dns.get('ok'), '解析成功 ' + str(dns.get('address')), '未解析')}")
            lines.append(f"  TCP 22: {_check(tcp.get('ok'), '可达 ' + str(tcp.get('latency_ms')) + ' ms', '不可达')}")
            lines.append(f"  SSH: {_check(ssh.get('ok'), '登录成功', '未登录')}")
            if status.get("auth_backend"):
                lines.append(f"  认证方式: {status['auth_backend']}")
        lines.append("")

        remote = status.get("remote")
        if remote:
            lines.append("[设备信息]")
            labels = [
                ("HOSTNAME", "主机名"),
                ("MODEL", "型号"),
                ("OS", "系统"),
                ("KERNEL", "内核"),
                ("ARCH", "架构"),
                ("IP", "IP 地址"),
                ("UPTIME", "运行秒数"),
                ("PYTHON", "Python"),
                ("LAUNCHER", "启动器"),
                ("APPS", "已装应用数"),
            ]
            for key, label in labels:
                if remote.get(key):
                    lines.append(f"  {label}: {remote[key]}")
            lines.append("")

    lines.append("[下一步]")
    for step in status.get("next_steps") or []:
        lines.append(f"  - {step}")
    lines.append("")
    lines.append("提示: 运行 `serve` 可以在浏览器中查看同一份状态面板。")
    return "\n".join(lines)


def _html(status: dict[str, Any]) -> str:
    env = status.get("environment") or {}
    device = status.get("device") or {}
    remote = status.get("remote") or {}
    dns = status.get("dns") or {}
    tcp = status.get("tcp") or {}
    ssh = status.get("ssh") or {}
    tools = env.get("tools") or {}
    mdns = env.get("mdns") or {}
    state = status.get("state", "unknown")
    state_class = {
        "connected": "ok",
        "unreachable": "bad",
        "auth_failed": "warn",
        "unconfigured": "warn",
    }.get(state, "unknown")

    def badge(ok: Any, on: str = "已连接", off: str = "未连接", unknown: str = "未检测") -> str:
        if ok is True:
            return f'<span class="badge ok">{on}</span>'
        if ok is False:
            return f'<span class="badge bad">{off}</span>'
        return f'<span class="badge unknown">{unknown}</span>'

    rows = []
    if device:
        rows.append(f"<tr><th>设备名称</th><td>{device.get('name', '-')}</td></tr>")
        rows.append(f"<tr><th>设备类型</th><td>{device.get('device_class', '-')}</td></tr>")
        rows.append(f"<tr><th>设备地址</th><td>{device.get('host', '-')}</td></tr>")
        rows.append(f"<tr><th>登录用户</th><td>{device.get('user', '-')}</td></tr>")
        rows.append(f"<tr><th>DNS</th><td>{badge(dns.get('ok'))} {dns.get('address') or ''}</td></tr>")
        rows.append(f"<tr><th>SSH 端口</th><td>{badge(tcp.get('ok'))} {tcp.get('latency_ms') or ''} ms</td></tr>")
        rows.append(f"<tr><th>SSH 登录</th><td>{badge(ssh.get('ok'))}</td></tr>")
    else:
        rows.append('<tr><th>设备</th><td><span class="badge unknown">尚未配置</span></td></tr>')

    info_rows = []
    info_labels = [
        ("HOSTNAME", "主机名"), ("MODEL", "型号"), ("OS", "系统"),
        ("KERNEL", "内核"), ("ARCH", "架构"), ("IP", "IP 地址"),
        ("UPTIME", "运行秒数"), ("PYTHON", "Python"),
        ("LAUNCHER", "启动器"), ("APPS", "已装应用数"),
    ]
    for key, label in info_labels:
        if remote.get(key):
            info_rows.append(f"<tr><th>{label}</th><td>{remote[key]}</td></tr>")

    tech_rows = [
        f"<tr><th>DNS</th><td>{dns.get('address') or '未解析'}</td></tr>",
        f"<tr><th>TCP 22</th><td>{'可达' if tcp.get('ok') else '不可达'}</td></tr>",
        f"<tr><th>SSH</th><td>{'已登录' if ssh.get('ok') else '未登录'}</td></tr>",
        f"<tr><th>认证方式</th><td>{status.get('auth_backend') or '-'}</td></tr>",
        f"<tr><th>ssh</th><td>{'可用' if tools.get('ssh') else '缺失'}</td></tr>",
        f"<tr><th>scp</th><td>{'可用' if tools.get('scp') else '缺失'}</td></tr>",
        f"<tr><th>sshpass</th><td>{'可用' if tools.get('sshpass') else '未安装（可选）'}</td></tr>",
        f"<tr><th>mDNS</th><td>{'可用' if any(mdns.values()) else '不可用（可选）'}</td></tr>",
    ]
    discovered = status.get("discovered") or []
    discovered_html = ""
    if discovered:
        discovered_html = "<h3>已发现的设备</h3><ul>" + "".join(
            f"<li>{item['name']} - {item['host']} ({item['device_class']})</li>"
            for item in discovered
        ) + "</ul>"

    return (
        HTML_TEMPLATE.replace("__STATE_CLASS__", state_class)
        .replace("__SUMMARY__", str(status.get("summary", "连接状态未知")))
        .replace("__ADVICE__", str(status.get("advice", "")))
        .replace("__DEVICE_ROWS__", "".join(rows))
        .replace(
            "__INFO_ROWS__",
            "".join(info_rows) or '<tr><td colspan="2">连接成功后可查看设备信息</td></tr>',
        )
        .replace("__TECH_ROWS__", "".join(tech_rows))
        .replace("__DISCOVERED__", discovered_html)
        .replace("__CHECKED_AT__", str(status.get("checked_at", "-")))
    )


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MaixPy Skill - 设备连接面板</title>
<style>
:root{--bg:#0f1117;--card:#171a23;--line:#2a2f3d;--text:#e8eaf0;--muted:#9aa3b2;--ok:#2ecc71;--bad:#e74c3c;--warn:#f1c40f;--accent:#4f8cff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
.wrap{max-width:980px;margin:0 auto;padding:24px 18px 60px}
h1{font-size:24px;margin:0 0 4px}
.sub{color:var(--muted);margin-bottom:20px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:760px){.grid{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin-bottom:16px}
.card h2{font-size:16px;margin:0 0 12px;color:#cfe0ff}
.card h3{font-size:14px;margin:14px 0 6px;color:#cfe0ff}
.status-ok{border-color:rgba(46,204,113,.55);background:rgba(46,204,113,.08)}
.status-bad{border-color:rgba(231,76,60,.55);background:rgba(231,76,60,.08)}
.status-warn{border-color:rgba(241,196,15,.45);background:rgba(241,196,15,.07)}
.status-unknown{border-color:rgba(154,163,178,.35)}
.status-title{font-size:20px;font-weight:600;margin-bottom:6px}
.status-advice{color:var(--muted)}
details summary{cursor:pointer;color:#cfe0ff;font-size:15px;margin-bottom:10px}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:7px 6px;border-bottom:1px solid var(--line);vertical-align:top}
th{width:34%;color:var(--muted);font-weight:500}
tr:last-child th,tr:last-child td{border-bottom:none}
.badge{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;margin-right:6px}
.ok{background:rgba(46,204,113,.15);color:var(--ok)}
.bad{background:rgba(231,76,60,.15);color:var(--bad)}
.unknown{background:rgba(241,196,15,.12);color:var(--warn)}
ul{margin:8px 0 0;padding-left:20px}
li{margin:6px 0;color:#dfe4ee}
code{background:#0b0d13;border:1px solid var(--line);border-radius:5px;padding:1px 6px;color:#9fd1ff}
.actions{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:6px}
button{background:var(--accent);border:0;color:white;padding:10px 18px;border-radius:7px;font-size:14px;cursor:pointer}
button:disabled{opacity:.55;cursor:default}
.foot{color:var(--muted);font-size:12px;margin-top:10px}
</style>
</head>
<body>
<div class="wrap">
<h1>MaixPy 设备连接</h1>
<div class="sub">最近检测：__CHECKED_AT__ ｜ 不显示密码</div>

<div class="card status-__STATE_CLASS__">
<div class="status-title">__SUMMARY__</div>
<div class="status-advice">__ADVICE__</div>
</div>

<div class="grid">
<div class="card">
<h2>设备</h2>
<table>__DEVICE_ROWS__</table>
__DISCOVERED__
</div>
<div class="card">
<h2>设备信息</h2>
<table>__INFO_ROWS__</table>
</div>
</div>

<details class="card">
<summary>技术详情（一般不需要查看）</summary>
<table>__TECH_ROWS__</table>
</details>

<div class="card">
<h2>建议</h2>
<p>__ADVICE__</p>
<div class="actions">
<button id="recheck" onclick="recheck()">重新检测</button>
<span id="msg" class="foot"></span>
</div>
</div>

<div class="foot">你只需要把设备 IP 告诉助手，连接和配置由助手完成。</div>
</div>
<script>
async function recheck(){
  const btn=document.getElementById('recheck');
  const msg=document.getElementById('msg');
  btn.disabled=true; msg.textContent='检测中，最长约 15 秒...';
  try{
    const r=await fetch('/api/check',{method:'POST'});
    if(!r.ok) throw new Error('HTTP '+r.status);
    msg.textContent='检测完成，正在刷新...';
    location.reload();
  }catch(e){
    msg.textContent='检测失败：'+e.message;
    btn.disabled=false;
  }
}
</script>
</body>
</html>
"""


def make_handler(cwd: Path | None, deep: bool) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, body: bytes, content_type: str, code: int = 200) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path in ("/", "/index.html"):
                status = collect(cwd=cwd, deep=deep)
                self._send(_html(status).encode("utf-8"), "text/html; charset=utf-8")
            elif self.path == "/api/status":
                status = collect(cwd=cwd, deep=deep)
                self._send(json.dumps(status, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            else:
                self.send_error(404)

        def do_POST(self) -> None:  # noqa: N802
            if self.path == "/api/check":
                status = collect(cwd=cwd, deep=True)
                self._send(json.dumps(status, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            else:
                self.send_error(404)

        def log_message(self, *args: Any) -> None:
            return

    return Handler


def serve(*, host: str = "127.0.0.1", port: int = 8765, cwd: Path | None = None, deep: bool = True) -> None:
    """Serve the dashboard until interrupted."""
    server = ThreadingHTTPServer((host, port), make_handler(cwd, deep))
    print(f"MaixPy dashboard: http://{host}:{port}/")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("警告: 面板绑定在非本机地址，同一网络的人都能查看设备状态（不包含密码）。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
