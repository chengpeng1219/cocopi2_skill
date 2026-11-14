"""One-shot deploy, run, frame capture, and launcher restore pipeline."""
from __future__ import annotations

import time
from pathlib import Path

from . import launcher, logs
from .config import Device
from .ssh import CommandResult, RemoteSession


RUN_WRAPPER = '''"""Run an app with display retry and optional first-frame capture."""
import os
import runpy
import sys
import time

from maix import display

_OriginalDisplay = display.Display
_saved = False


def _retrying_display(*args, **kwargs):
    last = None
    for _ in range(12):
        try:
            return _OriginalDisplay(*args, **kwargs)
        except Exception as exc:
            last = exc
            time.sleep(0.5)
    if last is not None:
        raise last
    return _OriginalDisplay(*args, **kwargs)


_capture_path = os.environ.get("MAIXPY_CAPTURE_PATH", "")
if _capture_path:
    _orig_show = _OriginalDisplay.show

    def _capturing_show(self, frame, *args, **kwargs):
        global _saved
        if not _saved:
            try:
                parent = os.path.dirname(_capture_path)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                frame.save(_capture_path)
                print("MAIXPY_SKILL_IMAGE", _capture_path, flush=True)
                _saved = True
            except Exception as exc:
                print("MAIXPY_CAPTURE_ERROR", repr(exc), flush=True)
        return _orig_show(self, frame, *args, **kwargs)

    _OriginalDisplay.show = _capturing_show

display.Display = _retrying_display

script = sys.argv[1] if len(sys.argv) > 1 else "main.py"
runpy.run_path(script, run_name="__main__")
'''


def _check(result: CommandResult, what: str) -> None:
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"{what} failed: {detail[:300]}")


def _wait_online(session: RemoteSession, timeout: int) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            result = session.run("echo __MAIXPY_ONLINE__", timeout=8)
        except Exception:
            result = None
        if result is not None and result.returncode == 0 and "__MAIXPY_ONLINE__" in result.stdout:
            return True
        time.sleep(2)
    return False


def _ensure_dev_mode(session: RemoteSession) -> str:
    try:
        status = session.run(launcher.status_command(), timeout=15)
    except Exception:
        status = None
    if status is not None and "daemon=stopped" in status.stdout and "launcher=stopped" in status.stdout:
        return "already"

    result = session.run(launcher.enter_command(), timeout=40)
    if "development_mode=entered" in result.stdout:
        return "entered"

    time.sleep(3)
    result = session.run(launcher.enter_command(), timeout=40)
    if "development_mode=entered" in result.stdout:
        return "entered_retry"
    raise RuntimeError(f"failed to enter development mode: {(result.stderr or result.stdout).strip()[:300]}")


def _pull_with_retry(session: RemoteSession, remote_path: str, local_path: Path, attempts: int = 8) -> bool:
    for _ in range(attempts):
        try:
            result = session.get(remote_path, str(local_path))
        except Exception:
            result = None
        if result is not None and result.returncode == 0 and local_path.exists() and local_path.stat().st_size > 0:
            return True
        time.sleep(0.5)
    return False


def _launch_detached(session: RemoteSession, remote_dir: str, env: str, log_name: str) -> str:
    pid_file = f"{remote_dir}/{log_name}.pid"
    command = (
        f"cd {remote_dir} && setsid -f sh -c 'echo $$ > {pid_file}; "
        f"exec env {env} python3 run_wrapper.py main.py' "
        f"> {log_name}.log 2>&1 < /dev/null"
    )
    session.run(command, timeout=10)
    time.sleep(0.5)
    result = session.run(f"cat {pid_file} 2>/dev/null", timeout=5)
    return result.stdout.strip()


def _kill_pid(session: RemoteSession, pid: str) -> None:
    if pid:
        session.run(f"kill {pid} 2>/dev/null || true", timeout=5)


def _deploy_once(
    device: Device,
    local_file: Path,
    *,
    timeout: int,
    background: bool,
    capture: bool,
    restore: bool,
    wait: float,
    cwd: Path | None,
    reconnect_timeout: int,
) -> Path:
    run_id = logs.new_run_id(local_file)
    out_dir = logs.create_run_dir(run_id, cwd)
    remote_dir = f"/tmp/maixpy_skill/{run_id}"
    summary: dict[str, object] = {
        "run_id": run_id,
        "local_file": str(local_file),
        "remote_dir": remote_dir,
        "background": background,
        "capture": capture,
    }

    session = RemoteSession(device, timeout=20)
    summary["backend"] = session.backend
    try:
        if not _wait_online(session, reconnect_timeout):
            raise RuntimeError("device is not reachable over SSH")

        summary["development_mode"] = _ensure_dev_mode(session)
        _check(session.run(f"mkdir -p {remote_dir}/artifacts", timeout=10), "create remote dir")
        _check(session.put(str(local_file), f"{remote_dir}/main.py"), "upload main.py")
        wrapper_path = out_dir / "run_wrapper.py"
        wrapper_path.write_text(RUN_WRAPPER, encoding="utf-8")
        _check(session.put(str(wrapper_path), f"{remote_dir}/run_wrapper.py"), "upload run wrapper")

        base_env = f"MAIXPY_SKILL_RUN_DIR={remote_dir}"

        if background:
            if capture:
                capture_env = (
                    f"{base_env} MAIXPY_DEBUG_IMAGES=1 "
                    f"MAIXPY_CAPTURE_PATH={remote_dir}/artifacts/frame.jpg"
                )
                capture_pid = _launch_detached(session, remote_dir, capture_env, "capture")
                summary["capture_pid"] = capture_pid
                time.sleep(wait)
                frame_path = out_dir / "frame.jpg"
                summary["frame"] = (
                    str(frame_path)
                    if _pull_with_retry(session, f"{remote_dir}/artifacts/frame.jpg", frame_path)
                    else None
                )
                _kill_pid(session, capture_pid)
                time.sleep(2)

            persistent_env = f"{base_env} MAIXPY_DEBUG_IMAGES=0"
            pid = _launch_detached(session, remote_dir, persistent_env, "run")
            summary["pid"] = pid
            summary["launch"] = f"PID={pid}"
            time.sleep(1)
        else:
            env = f"{base_env} MAIXPY_DEBUG_IMAGES={'1' if capture else '0'}"
            if capture:
                env += f" MAIXPY_CAPTURE_PATH={remote_dir}/artifacts/frame.jpg"
            command = f"cd {remote_dir} && timeout {timeout} env {env} python3 run_wrapper.py main.py"
            result = session.run(command, timeout=timeout + 20)
            summary["launch"] = result.stdout.strip()
            summary["exit_code"] = result.returncode
            if capture:
                frame_path = out_dir / "frame.jpg"
                summary["frame"] = (
                    str(frame_path)
                    if _pull_with_retry(session, f"{remote_dir}/artifacts/frame.jpg", frame_path)
                    else None
                )

        for log_name in ("capture", "run"):
            log_result = session.run(f"cat {remote_dir}/{log_name}.log 2>/dev/null", timeout=10)
            (out_dir / f"{log_name}.log").write_text(log_result.stdout, encoding="utf-8")
            if log_name == "run":
                summary["stdout_tail"] = log_result.stdout.strip()[-800:]

        if restore:
            _kill_pid(session, str(summary.get("pid") or ""))
            session.run(launcher.exit_command(), timeout=30)
            summary["restored"] = True
        return out_dir
    finally:
        session.close()
        logs.write_json(out_dir / "summary.json", summary)


def deploy_file(
    device: Device,
    local_file: Path,
    *,
    timeout: int = 60,
    background: bool = True,
    capture: bool = True,
    restore: bool = False,
    wait: float = 2.0,
    cwd: Path | None = None,
    reconnect_timeout: int = 45,
) -> Path:
    """Deploy one file, capture a frame, and optionally leave the app running."""
    local_file = local_file.resolve()
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            return _deploy_once(
                device,
                local_file,
                timeout=timeout,
                background=background,
                capture=capture,
                restore=restore,
                wait=wait,
                cwd=cwd,
                reconnect_timeout=reconnect_timeout,
            )
        except Exception as exc:
            last_error = exc
            if attempt == 0 and _looks_like_connection_error(exc):
                time.sleep(5)
                continue
            raise
    raise RuntimeError(str(last_error))


def _looks_like_connection_error(exc: Exception) -> bool:
    text = str(exc).lower()
    markers = (
        "timed out",
        "timeout",
        "unable to connect",
        "channel",
        "socket",
        "connection",
        "paramiko",
        "connection refused",
        "no route to host",
    )
    return any(marker in text for marker in markers)
