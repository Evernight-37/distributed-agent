# macOS 应用

发布构建的独立 Python 选择和验证见 [发布运行环境](RELEASE-RUNTIMES.md)。

当前源码版本为 2.0.0。Apple Silicon 构建已在 macOS 上验证；Intel Mac 应在 Intel 机器使用对应架构的运行时重新构建。

## 安装与升级

1. 退出旧版应用。
2. 将新版 `AgentHarness.app` 放到 `/Applications`，替换旧版后打开。
3. 在应用内解锁原有账户。

账户、团队数据库和设备配置保存在 `~/Library/Application Support/AgentHarness/data`。替换 `.app` 不会删除这些数据。应用退出后允许后台接收继续运行；托管方升级后应在设置中重启后台服务，使协作服务加载新代码。

员工卡片提供“设置”和“删除”。工作目录在项目派遣页按机器设置，不属于员工。删除前会显示受影响项目和活动任务；确认后停用员工并退出项目，保留历史消息和本机项目目录。运行任务中的员工暂时不能删除。

## 修复内容

- 群聊文件、拖入附件、粘贴截图、图片预览与下载；单文件最多 5 MB，客户端与托管端需使用兼容版本。
- Linux 服务器接收端可在 Mac 中管理，使用服务器自己的项目目录和 Codex 环境。
- 使用原生 Cocoa 窗口，并把可写状态放在用户数据目录。
- 保留后台解释器的执行权限，为私有目录设置 `700`、文件设置 `600`，修复锁文件权限错误。
- 完整打包 Python SSL 信任文件，修复登录时 `Errno 2: No such file or directory`。
- 从 Finder 启动也能发现应用包或 Homebrew 中的 Codex CLI。
- 使用 macOS 进程创建时间跟踪任务。
- 从 Tailscale 应用包调用命令行时设置 `TAILSCALE_BE_CLI=1`，避免把 GUI 启动输出当作 JSON 解析。
- 与 Windows 共享群聊、附件、员工管理、项目检查和实现任务能力。

## 从源码构建

使用与目标 Mac 架构一致的、可重定位的独立 Python 3.12 发行版。普通系统 Python、Homebrew Python 和虚拟环境不能作为后台运行时目录；构建用虚拟环境应从这份独立 Python 创建。

```bash
/path/to/standalone-python/bin/python3 -m venv .venv-macos
.venv-macos/bin/python -m pip install -r requirements-macos.txt
.venv-macos/bin/python scripts/build_macos.py \
  --python-runtime /path/to/standalone-python \
  --output-root /private/tmp/agentharness-release
```

产物为 `/private/tmp/agentharness-release/AgentHarness.app`。脚本验证独立运行时的 SSL、SQLite 和运行依赖，并执行 macOS 本地运行所需的 ad-hoc 签名；项目不要求开发者证书或公证。

构建后验证：

```bash
.venv-macos/bin/python -m unittest discover -s tests -t . -q
.venv-macos/bin/python tests/cleanroom_release.py \
  /private/tmp/agentharness-release/AgentHarness.app /private/tmp/agentharness-cleanroom
codesign --verify --deep --strict /private/tmp/agentharness-release/AgentHarness.app
```

可运行 `tests/gui_acceptance.py` 验证原生界面中的创建账户、默认管理员选项、员工创建与删除。测试使用独立临时数据，不需要实际账户密码。
