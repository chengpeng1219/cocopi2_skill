# MaixPy 设备工作流

配置 MaixCAM 系列设备、检查连接、进入开发模式、运行代码或诊断 helper 失败时阅读这个文件。

## Helper 命令

将 `<skill-dir>` 解析为已安装的 `cocopi2-skill` skill 文件夹。在本仓库中它是：

```text
tools/skills/cocopi2-skill
```

Linux/macOS 风格 shell：

```bash
python <skill-dir>/scripts/maixpy_skill/cli.py <command>
python3 <skill-dir>/scripts/maixpy_skill/cli.py <command>
```

Windows PowerShell：

```powershell
py -3 <skill-dir>\scripts\maixpy_skill\cli.py <command>
python <skill-dir>\scripts\maixpy_skill\cli.py <command>
```

从用户当前项目目录运行 helper，这样所有状态都会写入该项目的 `.maixpy/` 文件夹。

设置 `COCOPI2_CONFIG_DIR` 可以在多个项目间复用同一份设备配置。

## 连接面板

首次使用先看连接面板，不要靠猜：

```bash
python <skill-dir>/scripts/maixpy_skill/cli.py status
```

默认只输出一句状态、设备摘要和一条下一步建议；`--verbose` 才显示本机依赖与 DNS/TCP/SSH 详情，`--json` 输出原始结构。退出码：`0` 已连通，`1` 已配置但连不上，`2` 未配置。

网页版：

```bash
python <skill-dir>/scripts/maixpy_skill/cli.py serve --port 8765
```

打开 `http://127.0.0.1:8765/`。默认只监听本机且不会显示密码；`POST /api/check` 会重新检测。绑定非本机地址必须显式加 `--allow-lan`。

内部依赖（由助手处理，不要变成用户任务）：

- `ssh`/`scp` 必需；Windows 缺失时安装“可选功能 -> OpenSSH 客户端”。
- 不需要额外的 Python 插件。`sshpass` 可选；没有时请用户在自己的终端输入密码，或在具备 sshpass 的环境使用凭据文件。安装了 `paramiko` 时，可用于只读状态检查。
- mDNS 可选；没有时请用户从 `设置 -> 设备信息` 读取 IP。

## 首次使用

不要发问卷或命令清单。用户只需要提供设备 IP。

如果用户已经给了 IP，使用一条命令：

```bash
python <skill-dir>/scripts/maixpy_skill/cli.py connect --host <IP>
python <skill-dir>/scripts/maixpy_skill/cli.py status
```

`connect` 会尝试已保存/默认凭据，并从硬件型号自动识别设备类型。认证失败时再用 `auth`。如果没有保存过密码且本机缺少 paramiko/sshpass，`connect` 会停下并提示 `auth`，不会自行安装 pip 包。

如果不知道 IP，只问一句：

```text
请把 MaixCAM 屏幕“设置 -> 设备信息”里的 IP 告诉我，我来连接。
```

`device scan` 可以作为兜底，但不要让用户选择工具或阅读诊断。默认用户名是 `root`；除非登录失败，不要询问。

`status` 显示已连接后，直接进入功能开发：

```text
配置完成, 请描述你的功能
```

如果 `status` 报告失败，只转述成最多一个用户动作：

- 不可达：请用户确认同一 WiFi，并给出当前 IP。
- 认证失败：助手先尝试已保存凭据；只有自动认证不可用时，才请用户在自己的终端输入密码。

## 密码处理

只支持两种密码处理方式。

推荐先让用户在自己的终端运行：

```bash
python <skill-dir>/scripts/maixpy_skill/cli.py auth
```

助手无法自动打开终端或代你输入密码，用户必须在自己的终端运行 `auth`。它会无回显地提示密码，先验证登录，成功后才保存到 `.maixpy/device_credentials.local.json`。密码错误不会覆盖已有凭据；保存后会把 `.maixpy/` 加入项目 `.gitignore`。如果本机既没有 paramiko 也没有 sshpass，会在输入密码后自动使用当前 Python 安装 paramiko，最多等待 90 秒。不要要求用户把密码发到对话里。

### 方式一：本地凭据配置

helper 读取：

```text
.maixpy/device_credentials.local.json
```

如果文件不存在，从模板复制：

```bash
mkdir -p .maixpy
cp <skill-dir>/device_credentials.local.example.json .maixpy/device_credentials.local.json
```

Windows PowerShell：

```powershell
New-Item -ItemType Directory -Force .maixpy
Copy-Item <skill-dir>\device_credentials.local.example.json .maixpy\device_credentials.local.json
```

结构：

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

脚本读取密码后，如果本机安装了 `sshpass`，会通过环境变量 `SSHPASS` 传给 `sshpass -e`。不要把密码放进命令行参数。

### 方式二：终端输入

如果希望使用终端输入，让 `.maixpy/device_credentials.local.json` 中对应密码不存在或为空字符串。如果没有配置密码，或者本机没有 `sshpass`/paramiko，请用户在自己的终端运行 `auth` 命令并在提示处输入密码。

如果对话过程需要引导用户准备密码，只使用下面的提示，不要求用户把密码发到对话中：

```text
请输入你的密码(默认为:root)
```

MaixCAM2 使用：

```text
请输入你的密码(默认为:sipeed)
```

默认账号：

- MaixCAM / MaixCAM Pro：用户名 `root`，密码 `root`
- MaixCAM2：用户名 `root`，密码 `sipeed`

安全规则：

- 密码只写入 `.maixpy/device_credentials.local.json`。
- 永远不要把密码写入 `.maixpy/config.json`。
- 永远不要把密码放进命令行参数。
- 永远不要把密码写入生成代码、日志、总结或最终回复。
- 普通设备配置只保存非敏感字段：host、user、name、device class。
- 不要打印可能包含 secret 的环境变量。

## 运行代码

屏幕类应用优先使用一键部署流程：

```bash
python <skill-dir>/scripts/maixpy_skill/cli.py deploy <local.py>
```

它会进入一次开发模式，上传文件或目录，只运行一次应用，抓取真实画面，并让同一个进程继续运行。进度输出到 stderr（`--quiet` 可关闭），耗时写入 `summary.json`；安装/启动失败会自动恢复桌面。目录部署通过 `/tmp/maixpy_skill/cache/` 下的 SHA-256 清单只同步变化文件。汇报成功前先检查 `.maixpy/runs/<run_id>/frame.jpg`。需要停止正常应用并恢复桌面时加 `--restore`。目录部署用 `--entry` 指定入口，`--capture-timeout` 控制首帧等待时间；抓帧失败或后台进程退出时返回非零。`version` 查看版本，`update` 更新 git 检出。

通过 helper 运行用户代码：

```bash
python <skill-dir>/scripts/maixpy_skill/cli.py run <local.py>
```

如果是调试迭代，并且需要有意执行 `save_debug_image()`，运行：

```bash
python <skill-dir>/scripts/maixpy_skill/cli.py run <local.py> --debug-images
```

除非用户明确要求保留调试图片保存，否则最终验证不要使用 `--debug-images`。

helper 会：

1. 验证已配置设备。
2. 创建 `.maixpy/runs/<run_id>/`。
3. 进入并验证开发模式，因为用户可能在上一次运行后手动退出开发模式。
4. 运行代码前停止并复查 `/maixapp/apps/` 下正在运行的应用。
5. 上传脚本到 `/tmp/maixpy_skill/<run_id>/main.py`。
6. 在设备上设置 `MAIXPY_SKILL_RUN_DIR`。
7. 只有传入 `--debug-images` 时设置 `MAIXPY_DEBUG_IMAGES=1`；否则设置为 `0`。
8. 用检测到的 `python3` 或 `python` 运行。
9. 本地保存 stdout/stderr。
10. 如果可用，拉取 `/maixapp/tmp/last_run.log`。
11. 从设备 run 目录拉取图片产物。

每次运行后检查：

```text
.maixpy/runs/<run_id>/summary.json
.maixpy/runs/<run_id>/development-mode.log
.maixpy/runs/<run_id>/stdout.log
.maixpy/runs/<run_id>/stderr.log
.maixpy/runs/<run_id>/artifacts/
```

根据日志和图片决定下一次修改。

## 开发模式

使用：

```bash
python <skill-dir>/scripts/maixpy_skill/cli.py mode status
python <skill-dir>/scripts/maixpy_skill/cli.py mode enter
python <skill-dir>/scripts/maixpy_skill/cli.py mode exit
```

开发模式表示 `/maixapp/apps/launcher/launcher daemon` 和 `/maixapp/apps/launcher/launcher_daemon` 已停止，应用资源已释放，可直接运行代码。

`run` helper 每次运行代码前都会执行开发模式进入流程。进入开发模式必须：

1. 尝试杀死 `/maixapp/apps/launcher/launcher_daemon`。
2. 停止 `/maixapp/apps/` 下其他正在运行的应用。
3. 运行一个简短的 MaixPy `display.Display()` 刷新显示程序，以刷新屏幕并验证开发模式可用。

退出开发模式会在后台启动且只启动一个 launcher daemon。如果发现多个 daemon 进程，会先全部停止，避免争抢显示。

## 失败处理

- 如果 SSH 失败，运行 `device check`，报告准确连接错误，但不要暴露 secret。
- 如果无法停止 `/maixapp/apps/` 下正在运行的应用，不要盲目运行摄像头/屏幕代码。
- 如果 `mode enter` 已启动 daemon 但刷新显示失败，将其视为失败，并检查 stderr 或 `/tmp/launcher-daemon.log`。
- 如果 launcher 进程重复或桌面卡住，运行 `repair`。
- 用 `runs` 查看最近部署结果和帧图片路径。
- 如果命令看起来卡在“正在安装 paramiko”，先停止，再让用户在自己的终端运行 `auth`；当前版本会把安装限制在 90 秒。
- 如果应用卡住，检查生成代码是否有退出路径。
- 如果视觉任务输出不清楚，临时添加或启用调试图片保存，并用 `--debug-images` 重新运行。最终输出给用户前需要移除或关闭，除非用户明确要求保留调试模式。
