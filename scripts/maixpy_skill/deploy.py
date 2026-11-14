"""One-shot deploy, run, frame capture, and launcher restore pipeline."""
from __future__ import annotations

import tarfile
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
script_dir = os.path.dirname(os.path.abspath(script))
if script_dir:
    sys.path.insert(0, script_dir)
    os.chdir(script_dir)
runpy.run_path(os.path.basename(script), run_name="__main__")
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


def _wait_for_frame(
    session: RemoteSession,
    remote_path: str,
    local_path: Path,
    *,
    initial_wait: float,
    timeout: float,
) -> bool:
    deadline = time.time() + max(timeout, 0)
    if initial_wait > 0:
        time.sleep(min(initial_wait, max(timeout, 0)))
    while time.time() < deadline:
        if _pull_with_retry(session, remote_path, local_path, attempts=1):
            return True
        time.sleep(0.5)
    return _pull_with_retry(session, remote_path, local_path, attempts=1)


def _launch_detached(session: RemoteSession, remote_dir: str, env: str, script_path: str) -> str:
    pid_file = f"{remote_dir}/run.pid"
    command = (
        f"cd {remote_dir} && setsid -f sh -c 'echo $$ > {pid_file}; "
        f"exec env {env} python3 run_wrapper.py {script_path}' "
        f"> run.log 2>&1 < /dev/null"
    )
    session.run(command, timeout=10)
    time.sleep(0.5)
    result = session.run(f"cat {pid_file} 2>/dev/null", timeout=5)
    return result.stdout.strip()


def _process_alive(session: RemoteSession, pid: str) -> bool:
    if not pid:
        return False
    result = session.run(f"kill -0 {pid} 2>/dev/null && echo alive || echo dead", timeout=5)
    return "alive" in result.stdout


def _kill_pid(session: RemoteSession, pid: str) -> None:
    if pid:
        session.run(f"kill {pid} 2>/dev/null || true", timeout=5)


def _prepare_payload(
    local_file: Path,
    out_dir: Path,
    session: RemoteSession,
    remote_dir: str,
    entry: str,
) -> tuple[str, bool]:
    if local_file.is_dir():
        entry_path = local_file / entry
        if not entry_path.is_file():
            raise RuntimeError(f"entry file not found: {entry_path}")
        archive = out_dir / "app.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            for path in local_file.rglob("*"):
                if not path.is_file():
                    continue
                relative = path.relative_to(local_file)
                if any(part in {"__pycache__", ".git", ".maixpy"} for part in relative.parts):
                    continue
                if path.suffix == ".pyc":
                    continue
                tar.add(path, arcname=relative.as_posix())
        _check(session.put(str(archive), f"{remote_dir}/app.tar.gz"), "upload app archive")
        _check(
            session.run(
                f"mkdir -p {remote_dir}/app && tar -xzf {remote_dir}/app.tar.gz -C {remote_dir}/app",
                timeout=30,
            ),
            "extract app",
        )
        return f"app/{entry}", True

    _check(session.put(str(local_file), f"{remote_dir}/main.py"), "upload main.py")
    return "main.py", False


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
    capture_timeout: float,
    entry: str,
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
        wrapper_path = out_dir / "run_wrapper.py"
        wrapper_path.write_text(RUN_WRAPPER, encoding="utf-8")
        _check(session.put(str(wrapper_path), f"{remote_dir}/run_wrapper.py"), "upload run wrapper")

        script_path, is_directory = _prepare_payload(local_file, out_dir, session, remote_dir, entry)
        summary["directory"] = is_directory
        summary["entry"] = script_path

        env = f"MAIXPY_SKILL_RUN_DIR={remote_dir} MAIXPY_DEBUG_IMAGES=0"
        if capture:
            env += f" MAIXPY_CAPTURE_PATH={remote_dir}/artifacts/frame.jpg"

        if background:
            pid = _launch_detached(session, remote_dir, env, script_path)
            summary["pid"] = pid
            summary["launch"] = f"PID={pid}"
            summary["running"] = _process_alive(session, pid)
            if capture:
                frame_path = out_dir / "frame.jpg"
                if _wait_for_frame(
                    session,
                    f"{remote_dir}/artifacts/frame.jpg",
                    frame_path,
                    initial_wait=wait,
                    timeout=capture_timeout,
                ):
                    summary["frame"] = str(frame_path)
                summary["running"] = _process_alive(session, pid)
        else:
            command = f"cd {remote_dir} && timeout {timeout} env {env} python3 run_wrapper.py {script_path}"
            result = session.run(command, timeout=timeout + 20)
            summary["launch"] = result.stdout.strip()
            summary["exit_code"] = result.returncode
            summary["running"] = False
            if capture:
                frame_path = out_dir / "frame.jpg"
                if _pull_with_retry(session, f"{remote_dir}/artifacts/frame.jpg", frame_path):
                    summary["frame"] = str(frame_path)

        log_result = session.run(f"cat {remote_dir}/run.log 2>/dev/null", timeout=10)
        (out_dir / "run.log").write_text(log_result.stdout, encoding="utf-8")
        summary["stdout_tail"] = log_result.stdout.strip()[-800:]
        if capture and not summary.get("frame"):
            summary["capture_error"] = log_result.stdout.strip()[-500:]

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
    capture_timeout: float = 10.0,
    entry: str = "main.py",
) -> Path:
    """Deploy one file or directory, capture a frame, and optionally leave it running."""
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
                capture_timeout=capture_timeout,
                entry=entry,
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
