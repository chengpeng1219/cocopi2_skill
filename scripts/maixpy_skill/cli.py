from __future__ import annotations

import argparse
import getpass
import json
import shutil
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from maixpy_skill import dashboard, deploy, discover, doctor, launcher, runner
    from maixpy_skill.config import ConfigError, clear_device, get_device, load_config, save_device_password, set_device
    from maixpy_skill.ssh import RemoteSession, paramiko_available, run_ssh
else:
    from . import dashboard, deploy, discover, doctor, launcher, runner
    from .config import ConfigError, clear_device, get_device, load_config, save_device_password, set_device
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
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("auth", help="securely prompt for and save the device SSH password")
    p.add_argument("--host")
    p.add_argument("--user")
    p.add_argument("--name")
    p.add_argument("--device-class")
    p.set_defaults(func=cmd_auth)

    p = sub.add_parser("deploy", help="deploy one file, run it, and capture a frame")
    p.add_argument("file")
    p.add_argument("--timeout", type=int, default=60)
    p.add_argument("--no-background", action="store_true", help="run in foreground until timeout")
    p.add_argument("--no-capture", action="store_true", help="skip frame capture")
    p.add_argument("--restore", action="store_true", help="stop the app and restore the launcher when done")
    p.add_argument("--wait", type=float, default=2.0, help="seconds before capturing a background frame")
    p.add_argument("--reconnect-timeout", type=int, default=45)
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
    dashboard.serve(host=args.host, port=args.port, deep=not args.no_deep)
    return 0


def cmd_auth(args: argparse.Namespace) -> int:
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
    if raw is None:
        set_device(host, user, name, device_class)

    password = getpass.getpass(f"请输入 {user}@{host} 的 SSH 密码（输入不会显示）: ")
    if not password:
        print("未输入密码，已取消。")
        return 2

    save_device_password(host, password, name=name)
    print("密码已保存到 .maixpy/device_credentials.local.json。")

    if not paramiko_available() and not shutil.which("sshpass"):
        print("本机缺少 paramiko 和 sshpass，暂时无法自动验证。")
        print("请运行：python -m pip install paramiko")
        return 2

    device = get_device()
    try:
        session = RemoteSession(device, timeout=12)
        result = session.run("echo __AUTH_OK__", timeout=10)
        session.close()
    except Exception as exc:
        print(f"认证测试失败：{exc}")
        return 1

    if result.returncode == 0 and "__AUTH_OK__" in result.stdout:
        print("认证成功，设备已连接。")
        return 0
    detail = (result.stderr or result.stdout).strip()[:200]
    print(f"认证失败：{detail}")
    return 1


def cmd_deploy(args: argparse.Namespace) -> int:
    device = get_device()
    out_dir = deploy.deploy_file(
        device,
        Path(args.file),
        timeout=args.timeout,
        background=not args.no_background,
        capture=not args.no_capture,
        restore=args.restore,
        wait=args.wait,
        reconnect_timeout=args.reconnect_timeout,
    )
    print(f"DEPLOY_DIR={out_dir}")
    summary_path = out_dir / "summary.json"
    if summary_path.exists():
        print(summary_path.read_text(encoding="utf-8"), end="")
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
