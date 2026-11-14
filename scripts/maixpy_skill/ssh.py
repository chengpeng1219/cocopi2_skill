from __future__ import annotations

import os
import shutil
import shlex
import subprocess
from dataclasses import dataclass

from .config import Device


DEFAULT_SSH_OPTIONS = [
    "-o",
    "ConnectTimeout=8",
    "-o",
    "StrictHostKeyChecking=accept-new",
]


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


def quote_remote(command: str) -> str:
    return command


def ssh_command(device: Device, remote_command: str, *, tty: bool = False, batch_mode: bool = False) -> list[str]:
    cmd = ["ssh", *DEFAULT_SSH_OPTIONS]
    if batch_mode:
        cmd.extend(["-o", "BatchMode=yes"])
    if tty:
        cmd.append("-tt")
    cmd.extend([device.target, quote_remote(remote_command)])
    return with_password_helper(device, cmd)


def scp_to_command(device: Device, local_path: str, remote_path: str) -> list[str]:
    return with_password_helper(device, ["scp", *DEFAULT_SSH_OPTIONS, local_path, f"{device.target}:{remote_path}"])


def scp_from_command(device: Device, remote_path: str, local_path: str) -> list[str]:
    return with_password_helper(device, ["scp", *DEFAULT_SSH_OPTIONS, f"{device.target}:{remote_path}", local_path])


def with_password_helper(device: Device, command: list[str]) -> list[str]:
    if not device.password or not shutil.which("sshpass"):
        return command
    return ["sshpass", "-e", *command]


def command_env(device: Device, env: dict[str, str] | None = None) -> dict[str, str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    if device.password:
        merged_env["SSHPASS"] = device.password
    return merged_env


def run_ssh(
    device: Device,
    remote_command: str,
    *,
    timeout: int = 30,
    env: dict[str, str] | None = None,
    batch_mode: bool = False,
) -> CommandResult:
    if prefer_paramiko(device):
        client = None
        try:
            client = _connect_paramiko(device, timeout)
            return _run_paramiko(client, remote_command, timeout)
        except Exception as exc:
            return CommandResult(255, "", f"paramiko: {exc}")
        finally:
            if client is not None:
                client.close()
    proc = subprocess.run(
        ssh_command(device, remote_command, batch_mode=batch_mode),
        text=True,
        capture_output=True,
        timeout=timeout,
        env=command_env(device, env),
        check=False,
    )
    return CommandResult(proc.returncode, proc.stdout, proc.stderr)


def shell_join(command: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in command)


def paramiko_available() -> bool:
    try:
        import paramiko  # noqa: F401
        return True
    except ImportError:
        return False


def prefer_paramiko(device: Device) -> bool:
    return bool(device.password and not shutil.which("sshpass") and paramiko_available())


def _connect_paramiko(device: Device, timeout: int):
    import paramiko

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=device.host,
        username=device.user,
        password=device.password,
        timeout=timeout,
        look_for_keys=False,
        allow_agent=False,
    )
    return client


def _run_paramiko(client, remote_command: str, timeout: int) -> CommandResult:
    _, stdout, stderr = client.exec_command(remote_command, timeout=timeout)
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    code = stdout.channel.recv_exit_status()
    return CommandResult(code, out, err)


def put_file(device: Device, local_path: str, remote_path: str, *, timeout: int = 30) -> CommandResult:
    if prefer_paramiko(device):
        client = None
        try:
            client = _connect_paramiko(device, timeout)
            sftp = client.open_sftp()
            sftp.put(local_path, remote_path)
            sftp.close()
            return CommandResult(0, "", "")
        except Exception as exc:
            return CommandResult(1, "", f"paramiko sftp put: {exc}")
        finally:
            if client is not None:
                client.close()
    proc = subprocess.run(
        scp_to_command(device, local_path, remote_path),
        text=True,
        capture_output=True,
        env=command_env(device),
        check=False,
    )
    return CommandResult(proc.returncode, proc.stdout, proc.stderr)


def get_file(device: Device, remote_path: str, local_path: str, *, timeout: int = 30) -> CommandResult:
    if prefer_paramiko(device):
        client = None
        try:
            client = _connect_paramiko(device, timeout)
            sftp = client.open_sftp()
            sftp.get(remote_path, local_path)
            sftp.close()
            return CommandResult(0, "", "")
        except Exception as exc:
            return CommandResult(1, "", f"paramiko sftp get: {exc}")
        finally:
            if client is not None:
                client.close()
    proc = subprocess.run(
        scp_from_command(device, remote_path, local_path),
        text=True,
        capture_output=True,
        env=command_env(device),
        check=False,
    )
    return CommandResult(proc.returncode, proc.stdout, proc.stderr)


class RemoteSession:
    """Persistent remote session with paramiko/sshpass/ssh fallback."""

    def __init__(self, device: Device, *, timeout: int = 20):
        self.device = device
        self.timeout = timeout
        self.backend = "ssh"
        self._client = None
        if prefer_paramiko(device):
            self._client = _connect_paramiko(device, timeout)
            self.backend = "paramiko"
        elif device.password and shutil.which("sshpass"):
            self.backend = "sshpass"

    def run(self, command: str, *, timeout: int | None = None) -> CommandResult:
        if self._client is not None:
            return _run_paramiko(self._client, command, timeout or self.timeout)
        return run_ssh(self.device, command, timeout=timeout or self.timeout)

    def put(self, local_path: str, remote_path: str) -> CommandResult:
        if self._client is not None:
            sftp = self._client.open_sftp()
            try:
                sftp.put(local_path, remote_path)
            finally:
                sftp.close()
            return CommandResult(0, "", "")
        return put_file(self.device, local_path, remote_path)

    def get(self, remote_path: str, local_path: str) -> CommandResult:
        if self._client is not None:
            sftp = self._client.open_sftp()
            try:
                sftp.get(remote_path, local_path)
            finally:
                sftp.close()
            return CommandResult(0, "", "")
        return get_file(self.device, remote_path, local_path)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
