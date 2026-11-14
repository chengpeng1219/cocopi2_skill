# MaixPy Device Workflow

Read this file when configuring a MaixCAM-family device, checking connectivity, entering development mode, running code, or diagnosing helper failures.

## Helper Command

Resolve `<skill-dir>` to the installed `cocopi2-skill` skill folder. In this repository it is:

```text
tools/skills/cocopi2-skill
```

Linux/macOS-style shells:

```bash
python <skill-dir>/scripts/maixpy_skill/cli.py <command>
python3 <skill-dir>/scripts/maixpy_skill/cli.py <command>
```

Windows PowerShell:

```powershell
py -3 <skill-dir>\scripts\maixpy_skill\cli.py <command>
python <skill-dir>\scripts\maixpy_skill\cli.py <command>
```

Run helper commands from the user's current project directory so all state is stored under that project's `.maixpy/` folder.

## Connection Dashboard

Start first use with the dashboard instead of guessing what the user needs:

```bash
python <skill-dir>/scripts/maixpy_skill/cli.py status
```

Default output is one status line, a device summary, and one next action. `--verbose` exposes local requirements and DNS/TCP/SSH details; `--json` prints the raw structure. Exit codes are `0` (SSH connected), `1` (configured but unreachable), and `2` (not configured).

For a browser view, run:

```bash
python <skill-dir>/scripts/maixpy_skill/cli.py serve --port 8765
```

Then open `http://127.0.0.1:8765/`. The server binds to localhost by default and never renders passwords. `POST /api/check` re-runs the checks. Binding a non-loopback address requires `--allow-lan`.

Internal requirements (the assistant handles these; do not turn them into user tasks):

- `ssh`/`scp` are required; install the Windows OpenSSH Client optional feature if missing.
- No separate Python plugin is required. `sshpass` is optional; without it, type the password in the user's terminal or use a credential file where sshpass exists. `paramiko`, when installed, is used read-only for status checks.
- mDNS is optional; without it, ask for the IP from `设置 -> 设备信息`.

## First Use

Do not send a questionnaire or a command list. The user should only need to provide the device IP.

If the user already gave an IP, use the one-command path:

```bash
python <skill-dir>/scripts/maixpy_skill/cli.py connect --host <IP>
python <skill-dir>/scripts/maixpy_skill/cli.py status
```

`connect` tries saved/default credentials and detects the device class from the hardware model. If it reports authentication failure, use `auth`.

If the IP is unknown, ask exactly one question:

```text
请把 MaixCAM 屏幕“设置 -> 设备信息”里的 IP 告诉我，我来连接。
```

`device scan` may be used as a fallback, but do not make the user choose tools or read diagnostics. The default username is `root`; do not ask for it unless login fails.

When `status` reports connected, proceed directly:

```text
配置完成, 请描述你的功能
```

If `status` reports a failure, translate it into at most one user action:

- Unreachable: ask for the current IP and confirm the device and computer are on the same WiFi.
- Authentication failure: try stored credentials; only ask the user to type the password in their own terminal if automatic authentication is unavailable.

## Password Handling

Support exactly two password handling paths.

Preferred setup: ask the user to run this in their own terminal:

```bash
python <skill-dir>/scripts/maixpy_skill/cli.py auth
```

It prompts without echo, verifies the login first, and only then saves the password to `.maixpy/device_credentials.local.json`. A wrong password leaves existing credentials untouched. It also adds `.maixpy/` to the project `.gitignore`. If neither paramiko nor sshpass is installed, it automatically installs paramiko with the current Python interpreter, without confirmation. Do not ask the user to paste the password into chat.

### Path 1: Local credential config

The helper reads:

```text
.maixpy/device_credentials.local.json
```

If the file does not exist, copy the example:

```bash
mkdir -p .maixpy
cp <skill-dir>/device_credentials.local.example.json .maixpy/device_credentials.local.json
```

Windows PowerShell:

```powershell
New-Item -ItemType Directory -Force .maixpy
Copy-Item <skill-dir>\device_credentials.local.example.json .maixpy\device_credentials.local.json
```

Shape:

```json
{
  "defaults": {
    "maixcam-pro": {"user": "root", "password": "root"},
    "maixcam2": {"user": "root", "password": "sipeed"}
  },
  "devices": {
    "default": {"password": "root"},
    "maixcam2-4ea6": {"password": "sipeed"},
    "192.168.1.23": {"password": "root"}
  }
}
```

When a password is loaded from config and `sshpass` is installed, the helper uses `sshpass -e` and passes the value through `SSHPASS`. Never put passwords in command-line arguments.

### Path 2: Terminal input

To use terminal input, leave the matching password absent or empty in `.maixpy/device_credentials.local.json`. If no password is configured, or `sshpass`/paramiko is unavailable, ask the user to run `auth` in their own terminal and type the password there.

If the conversation needs to guide the user to prepare a password, use only these prompts and do not ask the user to paste the password into chat:

```text
请输入你的密码(默认为:root)
```

For MaixCAM2:

```text
请输入你的密码(默认为:sipeed)
```

Default credentials:

- MaixCAM / MaixCAM Pro: username `root`, password `root`
- MaixCAM2: username `root`, password `sipeed`

Security rules:

- Store passwords only in `.maixpy/device_credentials.local.json`.
- Never store passwords in `.maixpy/config.json`.
- Never put passwords in command-line arguments.
- Never write passwords to generated code, logs, summaries, or final replies.
- Store only non-secret device fields in normal config: host, user, name, and device class.
- Do not print environment variables that may contain secrets.

## Run Code

For screen-facing apps, prefer the one-shot deploy pipeline:

```bash
python <skill-dir>/scripts/maixpy_skill/cli.py deploy <local.py>
```

It enters development mode once, uploads a file or directory, runs the app once, captures a real frame, and leaves the same process running. Progress is printed to stderr (`--quiet` suppresses it), timings are stored in `summary.json`, and setup/launch failures restore the launcher automatically. Inspect `.maixpy/runs/<run_id>/frame.jpg` before reporting success. Use `--restore` to stop a healthy app and restore the launcher. Directory deployments use `--entry`; `--capture-timeout` controls the first-frame wait. Missing frames or dead background processes return a non-zero exit code.

Run user code through:

```bash
python <skill-dir>/scripts/maixpy_skill/cli.py run <local.py>
```

For debug iterations that intentionally execute `save_debug_image()`, run:

```bash
python <skill-dir>/scripts/maixpy_skill/cli.py run <local.py> --debug-images
```

Do not use `--debug-images` for final validation unless the user explicitly asks to keep debug image saving enabled.

The helper will:

1. Verify the configured device.
2. Create `.maixpy/runs/<run_id>/`.
3. Enter and verify development mode, because the user may have manually exited development mode since the previous run.
4. Stop and re-check any running applications under `/maixapp/apps/` before running code.
5. Upload the script to `/tmp/maixpy_skill/<run_id>/main.py`.
6. Set `MAIXPY_SKILL_RUN_DIR` on the device.
7. Set `MAIXPY_DEBUG_IMAGES=1` only when `--debug-images` is passed; otherwise set it to `0`.
8. Run with detected `python3` or `python`.
9. Save stdout/stderr locally.
10. Pull `/maixapp/tmp/last_run.log` if available.
11. Pull image artifacts from the device run directory.

After each run, inspect:

```text
.maixpy/runs/<run_id>/summary.json
.maixpy/runs/<run_id>/development-mode.log
.maixpy/runs/<run_id>/stdout.log
.maixpy/runs/<run_id>/stderr.log
.maixpy/runs/<run_id>/artifacts/
```

Use logs and images to decide the next edit.

## Development Mode

Use:

```bash
python <skill-dir>/scripts/maixpy_skill/cli.py mode status
python <skill-dir>/scripts/maixpy_skill/cli.py mode enter
python <skill-dir>/scripts/maixpy_skill/cli.py mode exit
```

Development mode means `/maixapp/apps/launcher/launcher daemon` and `/maixapp/apps/launcher/launcher_daemon` are stopped so app resources are free for direct code execution.

The `run` helper executes the development-mode entry flow before every code run. Entering development mode must:

1. Try to kill `/maixapp/apps/launcher/launcher_daemon`.
2. Stop any other running application under `/maixapp/apps/`.
3. Run a short MaixPy `display.Display()` refresh program to refresh the screen and verify development mode is usable.

Exiting development mode starts exactly one launcher daemon in the background. If multiple daemon processes are found, they are stopped first to avoid display contention.

## Failure Handling

- If SSH fails, run `device check` and report the exact connection failure without exposing secrets.
- If applications under `/maixapp/apps/` cannot be stopped, do not blindly run camera/display code.
- If `mode enter` reaches daemon startup but display refresh fails, treat it as failed and inspect stderr or `/tmp/launcher-daemon.log`.
- If launcher processes are duplicated or the desktop is stuck, run `repair`.
- Use `runs` to review recent deploy outcomes and frame paths.
- If the app hangs, inspect whether generated code has an exit path.
- If output is unclear for a vision task, temporarily add or enable debug image saving and rerun with `--debug-images`. Remove or disable it before presenting final user-facing code unless the user explicitly asks to keep debug mode enabled.
