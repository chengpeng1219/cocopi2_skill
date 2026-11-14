from __future__ import annotations

import argparse
import getpass
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from maixpy_skill import dashboard, deploy, discover, doctor, launcher, runner
    from maixpy_skill.config import (
        ConfigError,
        Device,
        clear_device,
        default_password,
        device_password,
        get_device,
        has_saved_password,
        load_config,
        normalize_device_class,
        save_device_password,
        set_device,
    )
    from maixpy_skill.ssh import RemoteSession, paramiko_available, run_ssh
else:
    from . import dashboard, deploy, discover, doctor, launcher, runner
    from .config import (
        ConfigError,
        Device,
        clear_device,
        default_password,
        device_password,
        get_device,
        has_saved_password,
        load_config,
        normalize_device_class,
        save_device_password,
        set_device,
    )
    from .ssh import RemoteSession, paramiko_available, run_ssh


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        if _is_missing_device_config(exc):
            print_device_setup_prompt(str(exc), python_cmd=Path(sys.argv[0]).as_posix())
            return 2
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cocopi2-skill")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("doctor")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("status", aliases=["setup"], help="show connection dashboard and setup guidance")
    p.add_argument("--json", action="store_true", help="print the raw status JSON")
    p.add_argument("--no-deep", action="store_true", help="skip network and SSH checks")
    p.add_argument("--verbose", action="store_true", help="show full technical diagnostics")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("serve", help="serve a local web connection dashboard")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-deep", action="store_true", help="skip network and SSH checks")
    p.add_argument("--allow-lan", action="store_true", help="allow binding the dashboard to a non-loopback address")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("auth", help="securely prompt for and save the device SSH password")
    p.add_argument("--host")
    p.add_argument("--user")
    p.add_argument("--name")
    p.add_argument("--device-class")
    p.set_defaults(func=cmd_auth)

    p = sub.add_parser("connect", help="connect to a device, detect its class, and save it")
    p.add_argument("--host")
    p.add_argument("--user")
    p.add_argument("--name")
    p.add_argument("--device-class")
    p.set_defaults(func=cmd_connect)

    p = sub.add_parser("repair", help="clean duplicate launcher daemons and restore one")
    p.set_defaults(func=cmd_repair)

    p = sub.add_parser("runs", help="list recent deploy runs")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_runs)

    p = sub.add_parser("version", help="show the installed skill version")
    p.set_defaults(func=cmd_version)

    p = sub.add_parser("update", help="update a git checkout of this skill")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("deploy", help="deploy one file, run it, and capture a frame")
    p.add_argument("file")
    p.add_argument("--timeout", type=int, default=60)
    p.add_argument("--no-background", action="store_true", help="run in foreground until timeout")
    p.add_argument("--no-capture", action="store_true", help="skip frame capture")
    p.add_argument("--restore", action="store_true", help="stop the app and restore the launcher when done")
    p.add_argument("--wait", type=float, default=2.0, help="seconds before capturing a background frame")
    p.add_argument("--reconnect-timeout", type=int, default=45)
    p.add_argument("--capture-timeout", type=float, default=10.0, help="maximum seconds to wait for a frame")
    p.add_argument("--entry", default="main.py", help="entry file inside a directory deployment")
    p.add_argument("--quiet", action="store_true", help="suppress progress messages")
    p.set_defaults(func=cmd_deploy)

    p = sub.add_parser("device")
    dsub = p.add_subparsers(dest="device_command", required=True)
    g = dsub.add_parser("get")
    g.set_defaults(func=cmd_device_get)
    s = dsub.add_parser("set")
    s.add_argument("--host", required=True)
    s.add_argument("--user", required=True)
    s.add_argument("--name", default="default")
    s.add_argument("--device-class", default="unknown")
    s.set_defaults(func=cmd_device_set)
    c = dsub.add_parser("clear")
    c.set_defaults(func=cmd_device_clear)
    scan = dsub.add_parser("scan")
    scan.set_defaults(func=cmd_device_scan)
    check = dsub.add_parser("check")
    check.set_defaults(func=cmd_device_check)

    p = sub.add_parser("mode")
    msub = p.add_subparsers(dest="mode_command", required=True)
    for name in ["status", "enter", "exit"]:
        m = msub.add_parser(name)
        m.set_defaults(func=cmd_mode)

    p = sub.add_parser("run")
    p.add_argument("file")
    p.add_argument("--timeout", type=int, default=60)
    p.add_argument("--debug-images", action="store_true", help="enable debug image saving for this run")
    p.set_defaults(func=cmd_run)
    return parser


def cmd_doctor(args: argparse.Namespace) -> int:
    result = doctor.check()
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


def cmd_status(args: argparse.Namespace) -> int:
    status = dashboard.collect(deep=not args.no_deep)
    if args.json:
        print(json.dumps(status, indent=2, sort_keys=True, ensure_ascii=False))
    else:
        print(dashboard.render_text(status, verbose=args.verbose))
    if (status.get("ssh") or {}).get("ok"):
        return 0
    return 1 if status.get("configured") else 2


def cmd_serve(args: argparse.Namespace) -> int:
    if not _is_loopback(args.host) and not args.allow_lan:
        print("为避免局域网暴露，serve 默认只允许本机。确实需要局域网访问请加 --allow-lan。")
        return 2
    dashboard.serve(host=args.host, port=args.port, deep=not args.no_deep)
    return 0


def _is_loopback(host: str) -> bool:
    return host in {"127.0.0.1", "localhost", "::1"}


def cmd_auth(args: argparse.Namespace) -> int:
    if not sys.stdin.isatty():
        print("当前不是交互式终端。助手无法自动打开终端或代你输入密码。")
        print("请在你自己的电脑终端运行 auth，或手动编辑 .maixpy/device_credentials.local.json。")
        return 2

    data = load_config()
    current = data.get("current_device")
    raw = data.get("devices", {}).get(current) if current else None
    host = args.host or (raw or {}).get("host")
    if not host:
        print("未配置设备。请在终端运行 auth --host <IP>，或先运行 device set。")
        return 2

    user = args.user or (raw or {}).get("user") or "root"
    device_class = args.device_class or (raw or {}).get("device_class") or "maixcam2"
    name = args.name or current or host

    try:
        password = getpass.getpass(f"请输入 {user}@{host} 的 SSH 密码（输入不会显示）: ")
    except (EOFError, KeyboardInterrupt):
        print("\n已取消。")
        return 2
    if not password:
        print("未输入密码，已取消。")
        return 2

    if not paramiko_available() and not shutil.which("sshpass"):
        print("本机缺少 paramiko 和 sshpass，正在自动安装 paramiko...")
        if not install_paramiko():
            print("请手动运行：python -m pip install paramiko")
            return 2

    test_device = Device(
        host=host,
        user=user,
        name=name,
        device_class=normalize_device_class(device_class),
        password=password,
    )
    try:
        session = RemoteSession(test_device, timeout=12)
        result = session.run("echo __AUTH_OK__", timeout=10)
        session.close()
    except Exception as exc:
        print(f"认证测试失败：{exc}")
        print("密码未保存，旧凭据保持不变。")
        return 1

    if result.returncode == 0 and "__AUTH_OK__" in result.stdout:
        try:
            set_device(host, user, name, device_class)
            save_device_password(host, password, name=name)
        except Exception as exc:
            print(f"认证成功，但保存配置失败：{exc}")
            return 1
        print("认证成功，密码已保存，设备已连接。")
        return 0
    detail = (result.stderr or result.stdout).strip()[:200]
    print(f"认证失败：{detail}")
    print("密码未保存，旧凭据保持不变。")
    return 1


def install_paramiko() -> bool:
    """Install paramiko with the current Python interpreter."""
    print("正在安装 paramiko（最多等待 90 秒）...")
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--timeout",
        "15",
        "--retries",
        "1",
        "paramiko",
    ]
    try:
        proc = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=90,
            check=False,
        )
    except Exception as exc:
        print(f"自动安装 paramiko 失败：{exc}")
        return False

    if proc.returncode == 0:
        print("已自动安装 paramiko。")
        return True

    print("自动安装 paramiko 失败。")
    tail = (proc.stderr or proc.stdout).strip().splitlines()[-4:]
    if tail:
        print("\n".join(tail))
    return False


def _auth_hint() -> str:
    cli = Path(__file__).resolve()
    return (
        "认证未完成。助手无法自动打开终端或代你输入密码。"
        f"请在你自己的电脑终端运行：python \"{cli}\" auth"
    )


def cmd_connect(args: argparse.Namespace) -> int:
    data = load_config()
    current = data.get("current_device")
    raw = data.get("devices", {}).get(current) if current else None
    host = args.host or (raw or {}).get("host")
    if not host:
        print("请把 MaixCAM 的 IP 告诉我，例如：connect --host 192.168.0.64")
        return 2

    user = args.user or (raw or {}).get("user") or "root"
    name = args.name or current or host
    device_class = args.device_class or (raw or {}).get("device_class") or "unknown"

    password = None
    if raw is not None and raw.get("host") == host:
        try:
            password = get_device().password
        except ConfigError:
            password = None
    if not password:
        password = device_password(name, host, normalize_device_class(device_class))
    if not password:
        password = default_password(device_class)

    saved = has_saved_password(name, host, normalize_device_class(device_class))
    backend_available = paramiko_available() or bool(shutil.which("sshpass"))
    if not saved and not backend_available:
        print("还没有保存过认证信息，且本机缺少 paramiko/sshpass。")
        print(_auth_hint())
        return 2

    if not backend_available:
        if not saved:
            print(_auth_hint())
            return 2
        print("已找到保存的凭据，正在自动安装 paramiko...")
        if not install_paramiko():
            return 2

    test_device = Device(
        host=host,
        user=user,
        name=name,
        device_class=normalize_device_class(device_class),
        password=password or None,
    )
    command = (
        "echo __OK__; "
        "echo \"HOSTNAME=$(hostname)\"; "
        "echo \"MODEL=$(tr -d '\\000' < /proc/device-tree/model 2>/dev/null)\"; "
        "echo \"ARCH=$(uname -m)\""
    )
    try:
        session = RemoteSession(test_device, timeout=12)
        result = session.run(command, timeout=10)
        session.close()
    except Exception as exc:
        print(f"连接失败：{exc}")
        return 1

    if result.returncode != 0 or "__OK__" not in result.stdout:
        detail = (result.stderr or result.stdout).strip()[:200]
        if "permission denied" in detail.lower() or "publickey" in detail.lower() or not detail:
            print(_auth_hint())
        else:
            print(f"连接失败：{detail}")
        return 1

    info: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" in line and line.split("=", 1)[0].isupper():
            key, value = line.split("=", 1)
            info[key] = value.strip()

    model = info.get("MODEL", "")
    detected = normalize_device_class(device_class)
    if "maixcam2" in model.lower():
        detected = "maixcam2"
    elif "maixcam" in model.lower():
        detected = "maixcam-pro"
    set_device(host, user, name, detected)

    print(f"已连接：{info.get('HOSTNAME', host)}（{host}）")
    if model:
        print(f"型号：{model}")
    if info.get("ARCH"):
        print(f"架构：{info['ARCH']}")
    print("可以直接告诉我要开发的功能。")
    return 0


def cmd_repair(args: argparse.Namespace) -> int:
    device = get_device()
    try:
        session = RemoteSession(device, timeout=12)
        result = session.run(launcher.start_launcher_daemon_command(), timeout=40)
        status = session.run(launcher.status_command(), timeout=15)
        session.close()
    except Exception as exc:
        print(f"修复失败：{exc}")
        return 1

    text = (result.stdout or result.stderr).strip()
    if text:
        print(text)
    for line in status.stdout.splitlines():
        if line.startswith(("daemon=", "launcher=", "daemon_count=")):
            print(line)
    return 0 if "daemon=running" in status.stdout else 1


def cmd_runs(args: argparse.Namespace) -> int:
    root = Path.cwd() / ".maixpy" / "runs"
    if not root.exists():
        print("暂无运行记录。")
        return 0
    runs = sorted(
        (path for path in root.iterdir() if path.is_dir()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )[: max(1, args.limit)]
    if not runs:
        print("暂无运行记录。")
        return 0
    for path in runs:
        summary: dict[str, object] = {}
        summary_path = path / "summary.json"
        if summary_path.exists():
            try:
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                summary = {}
        if summary.get("restored"):
            state = "restored"
        elif summary.get("running"):
            state = "running"
        elif "exit_code" in summary:
            state = f"exit={summary.get('exit_code')}"
        else:
            state = "unknown"
        stamp = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        frame = summary.get("frame") or "-"
        print(f"{stamp}  {path.name}  {state}  frame={frame}")
    return 0


def _skill_root() -> Path:
    return Path(__file__).resolve().parents[2]


def cmd_version(args: argparse.Namespace) -> int:
    root = _skill_root()
    version_file = root / "VERSION"
    version = version_file.read_text(encoding="utf-8").strip() if version_file.exists() else "unknown"
    print(f"cocopi2-skill {version}")
    print(f"路径: {root}")
    if (root / ".git").exists():
        try:
            commit = subprocess.check_output(
                ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                text=True,
            ).strip()
            print(f"提交: {commit}")
        except Exception:
            pass
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    root = _skill_root()
    if not (root / ".git").exists():
        print("当前安装不是 git 仓库。请从 https://github.com/chengpeng1219/cocopi2_skill 重新安装。")
        return 1
    proc = subprocess.run(
        ["git", "-C", str(root), "pull", "--ff-only"],
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.stdout.strip():
        print(proc.stdout.strip())
    if proc.stderr.strip():
        print(proc.stderr.strip())
    return proc.returncode


def cmd_deploy(args: argparse.Namespace) -> int:
    device = get_device()
    progress = None
    if not args.quiet:
        def progress(message: str) -> None:
            print(message, file=sys.stderr, flush=True)
        progress("连接设备...")
    out_dir = deploy.deploy_file(
        device,
        Path(args.file),
        timeout=args.timeout,
        background=not args.no_background,
        capture=not args.no_capture,
        restore=args.restore,
        wait=args.wait,
        reconnect_timeout=args.reconnect_timeout,
        capture_timeout=args.capture_timeout,
        entry=args.entry,
        progress=progress,
    )
    print(f"DEPLOY_DIR={out_dir}")
    summary_path = out_dir / "summary.json"
    summary: dict[str, object] = {}
    if summary_path.exists():
        text = summary_path.read_text(encoding="utf-8")
        print(text, end="")
        try:
            summary = json.loads(text)
        except json.JSONDecodeError:
            summary = {}
    if not args.no_capture and not summary.get("frame"):
        print("抓帧失败：应用没有显示画面或未在限定时间内显示。")
        tail = summary.get("capture_error") or summary.get("stdout_tail")
        if tail:
            print("最近日志：")
            print(str(tail).strip()[-1000:])
        return 1
    if not args.no_background and not summary.get("running"):
        print("后台进程未存活，请检查 run.log。")
        tail = summary.get("stdout_tail")
        if tail:
            print("最近日志：")
            print(str(tail).strip()[-1000:])
        return 1
    if args.no_background and summary.get("exit_code") not in (0, 124):
        return 1
    return 0


def cmd_device_get(args: argparse.Namespace) -> int:
    print(json.dumps(load_config(), indent=2, sort_keys=True))
    return 0


def cmd_device_set(args: argparse.Namespace) -> int:
    device = set_device(args.host, args.user, args.name, args.device_class)
    print(json.dumps({"ok": True, "device": device.public_dict()}, indent=2, sort_keys=True))
    return 0


def cmd_device_clear(args: argparse.Namespace) -> int:
    clear_device()
    print("device config cleared")
    return 0


def cmd_device_scan(args: argparse.Namespace) -> int:
    devices = [device.__dict__ for device in discover.scan_candidates()]
    print(json.dumps({"devices": devices}, indent=2, sort_keys=True))
    return 0 if devices else 1


def _is_missing_device_config(exc: ConfigError) -> bool:
    text = str(exc)
    return text == "no current device configured" or text.startswith("device not found:")


def print_device_setup_prompt(reason: str, *, python_cmd: str) -> None:
    print(f"ERROR: {reason}", file=sys.stderr)
    print("未配置设备。请把 MaixCAM 屏幕“设置 -> 设备信息”里的 IP 告诉我，我来连接。")
    devices = discover.scan_candidates()
    if devices:
        print("\n已发现以下设备，可直接确认其中一台：")
        for index, device in enumerate(devices, start=1):
            print(f"{index}. {device.name}  host={device.host}  type={device.device_class}")
    print(f"\n备用手动命令：python3 {python_cmd} device set --host <IP> --user root --device-class <maixcam-pro|maixcam2>")


def cmd_device_check(args: argparse.Namespace) -> int:
    device = get_device()
    result = run_ssh(device, "echo connected && uname -a", timeout=12)
    print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")
    if result.returncode == 0:
        print("配置完成, 请描述你的功能")
    return result.returncode


def cmd_mode(args: argparse.Namespace) -> int:
    device = get_device()
    commands = {
        "status": launcher.status_command(),
        "enter": launcher.enter_command(),
        "exit": launcher.exit_command(),
    }
    result = run_ssh(device, commands[args.mode_command], timeout=15)
    print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")
    return result.returncode


def cmd_run(args: argparse.Namespace) -> int:
    device = get_device()
    out_dir = runner.run_file(device, Path(args.file), timeout=args.timeout, debug_images=args.debug_images)
    print(f"RUN_DIR={out_dir}")
    summary = out_dir / "summary.json"
    if summary.exists():
        print(summary.read_text(encoding="utf-8"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
