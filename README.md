# Agent Harness

Agent Harness 是面向小型可信团队的分布式 Agent 协作应用。每个人管理自己电脑上的 Codex、模型、目录和执行权限；项目服务负责成员、会话、共享记忆与任务路由。

## 使用应用

当前稳定版为 **2.1.0**。从 [GitHub Releases](https://github.com/Evernight-37/distributed-agent/releases/latest) 下载与系统和 CPU 架构相符的完整发布包。

| 使用环境 | 下载文件 | 使用方法 |
| --- | --- | --- |
| Windows x64（推荐安装） | `AgentHarness-2.1.0-windows-x64-setup.exe` | 双击并按向导安装，然后从开始菜单启动 Agent Harness |
| Windows x64（便携） | `AgentHarness-2.1.0-windows-x64.zip` | 完整解压到独立目录，双击其中的 `AgentHarness.exe`；不要只复制 EXE |
| macOS Apple Silicon | `AgentHarness-2.1.0-macos-arm64.zip` | 解压后把 `AgentHarness.app` 放入“应用程序”并打开；本项目未做 Apple 签名或公证，系统可能要求在隐私与安全性中确认打开 |
| Linux x64 / ARM64 服务器 | `AgentHarness-2.1.0-linux-x64.tar.gz` 或 `AgentHarness-2.1.0-linux-arm64.tar.gz` | 使用无界面接收端；优先把包内的 `交给AI安装.md` 交给服务器 Codex，或阅读 [Linux 说明](docs/LINUX.md) |

发布包自带 Harness 所需运行时，不要求另外安装 Python、pip 或 venv。Windows 系统需要 Edge WebView2；每台实际执行 Agent 的机器仍需安装并登录 Codex。升级程序不会自动迁移或删除项目工作目录，账户数据保存在应用目录之外；退出旧版后安装或解压新版即可继续使用原账户数据。Windows 便携版应保留完整目录，Linux 端使用并排升级和回滚机制。

第一次使用请先阅读 [按钮式新手教程](docs/QUICKSTART.md)。教程从 Tailscale 准备开始，分别列出群主、服务托管者和受邀同事应点击的按钮，并在每一步说明成功标志与常见错误。单机使用可以跳过 Tailscale。多人协作的主流程是：

1. 群主从 Tailscale 管理台邀请外部用户，双方确认设备在线。
2. 每个人分别创建自己的 Harness 账户；不要共享账户或设备关联码。
3. 群主在应用里创建团队入口，并分享 HTTPS 团队地址和项目邀请码。
4. 同事凭两项信息加入项目，再把自己的机器和 Agent 接入该服务。
5. 每个人配置和派遣自己的 Agent；群主选择管理员 Agent。

Linux 服务器不要求用户手工照抄安装命令：桌面应用 **账户 → 添加 Linux 服务器** 可以生成带团队地址的安全配置提示词，直接粘贴给服务器 Codex。Linux 发布包也自带 `交给AI安装.md`；详见 [AI 配置流程](docs/AI-SETUP-LINUX.md)。

同一账户可以关联多台电脑。Windows 数据位于 `%LOCALAPPDATA%\AgentHarness\data`，macOS 数据位于 `~/Library/Application Support/AgentHarness/data`；项目工作目录、Codex 登录、设备私钥和研究环境不会自动同步。

## 在 Codex 中协作

登录应用后，打开 **设置 → 在 Codex 中使用协作**，点击“连接到 Codex”。此后可以在普通 Codex 对话中列出 Harness 项目、读取有权查看的会话、发送消息，以及给自己名下的 Agent 派发异步任务。

该入口使用本机 stdio MCP，不开放额外端口。Codex 获得受限委托身份，不能邀请成员、修改管理员、控制别人的机器或批准执行命令。

## 多人和多机连接

项目数据保存在创建项目时选择的协作服务上。托管电脑可在 **设置 → 协作服务 → 创建团队入口** 中建立 Tailscale 私有 HTTPS 地址。同事加入同一 Tailscale 网络后，用该地址和项目邀请码加入项目；不需要为每个团队重新创建账户。Tailscale 显示的 `100.x.x.x` 是机器地址，不是团队服务地址；应用中可分享的地址形如 `https://电脑名.xxxx.ts.net`。

团队地址是 Desktop 与设备接收端使用的服务入口，不再提供浏览器登录工作台。直接在浏览器打开时只显示迁移说明；人类操作请使用 Agent Harness 应用，Codex 通过本机 MCP 协作。

项目没有中心云、遥测或维护者账户服务。账户、项目和消息只保存在参与者自己的电脑及项目选定的协作服务上；应用不会安装组网软件、开放防火墙或接管 Codex/GitHub 凭据。协作服务的机器主人能够读取其数据库中的项目内容，因此成员应信任托管者。

侧边栏的 **文件速传** 是独立于项目和 Agent 的机器间大文件通道，不设置固定文件大小上限。它会分块续传并验证 SHA-256；实际可传大小取决于源文件系统、接收磁盘和临时中转服务的可用空间。同一账户的机器使用目标机预设目录自动接收；不同用户之间先发送请求，由接收者选择保存位置并确认。双方需共同接入至少一个协作服务，文件只在该服务临时中转，接收完成即清除。中转目前不是端到端加密，服务托管者可读取传输中的明文文件，因此仍应选择双方信任的托管者。聊天中的项目小附件仍按消息保存，每个最大 5 MB，不走这套确认流程。

同一账户的 Windows 或 macOS 控制台也可以选择 Linux 无界面节点作为接收机器。Linux 使用本机设备凭据在后台领取文件，离线后恢复连接会继续接收。账户所有者可从任意桌面控制台为同账户远程机器填写已经存在的绝对接收目录；路径只在签名控制指令投递期间经过可信协作服务，目标机器处理后会从服务记录中抹除。未设置的 Linux 使用接收端本地 `received-files`。当前 Linux 端尚不提供主动选择服务器文件并发送的 CLI。

服务托管者可在 **设置 → 协作服务** 备份或恢复项目数据库。备份使用 SQLite 一致快照；恢复只接受当前账户拥有且完整性检查通过的 Harness 数据库，并会在替换前自动保存当前数据库。正在传输的临时文件不属于项目备份；恢复不覆盖各机器上的项目工作目录、Codex 登录或本机 Agent 设置。

工作目录属于“项目 × 机器”，不属于 Agent。机器主人在第一次向项目派遣本机 Agent 时选择目录；同机同项目的多个 Agent 共享该目录，但会话和项目记忆仍按项目隔离。实现任务保持单写入保护，讨论和审阅任务可共享读取。删除已派遣 Agent 前会列出受影响项目和未完成任务；确认后撤回全部派遣并保留历史。

员工设置可启用“实现任务安全模式”。启用后，implement 任务必须使用干净的 linked Git worktree，并按任务中每行一个相对路径的 `allowed_files` 检查 Git 跟踪和普通未跟踪改动；越界或 HEAD 改变会让任务失败，但 Harness 不会擅自 reset 或删除文件。Git ignored 内容不在该轻量门禁内，所以它是防误操作措施，不是文件系统沙箱。未启用时，“声明修改范围”仍只是任务合同，不是文件系统 ACL。高敏项目还应使用专用普通系统用户，不要给 Agent 免密 sudo 或传统 Docker socket。

## 本机身份与恢复

身份先由本地解锁密码加密，再优先使用系统安全存储保护：Windows DPAPI、macOS Keychain、Linux Secret Service；平台服务不可用时保留 Scrypt + AES-GCM 加密文件后备。密钥不会上传到 Harness 中心云（本项目没有中心云）。

根设备可在 **账户 → 创建离线恢复包** 导出加密恢复包。请使用独立密码并离线保存。导入时只在内存中解密根材料，随后为新电脑生成新的子设备身份；恢复包不包含服务地址、项目数据或消息。普通关联电脑不能导出根恢复包。

## 从源码验证和构建

运行测试：

```powershell
python -m pip install -r requirements-dev.txt
npm ci
ruff check src scripts tests
ruff format --check src scripts tests
python -m unittest discover -s tests -t . -v
npm run format:check
npm test
```

`requirements-dev.txt` 会以 editable 模式安装当前 `agent_harness` 包；测试不依赖手工设置 `PYTHONPATH` 或修改 `sys.path`。

构建 Windows x64 便携包：

```powershell
python -m pip install -r requirements-build.txt
python scripts/build_desktop.py
```

构建结果位于 `dist/AgentHarness`。构建脚本下载并校验 Python.org 的嵌入式 Python 3.14.7，为后台接收端安装固定版本的 `cryptography`，然后生成完整便携目录。发布前还应运行：

```powershell
python tests/cleanroom_release.py dist/AgentHarness build/cleanroom
```

若要同时运行便携包接收端集成测试，请把 `AGENT_HARNESS_TEST_BUNDLE` 指向已构建的 `AgentHarness` 目录，再运行 `python -m unittest tests.test_desktop.PackagedReceiverTests -v`。

运行数据、令牌、密钥、日志、构建缓存和发布包均由 `.gitignore` 排除。发布流水线只负责生成并上传 Windows per-user 安装器、Windows 便携包、Linux x64/ARM64 包和 macOS ZIP；项目不包含平台证书、公证或应用内自动更新体系。用户从项目的 GitHub Releases 手动下载和替换程序，持久数据保存在应用目录之外。

## 源码目录

```text
assets/web/          Desktop WebView、新手教程与团队地址迁移页
docs/                用户文档与架构说明
scripts/             构建、打包等维护脚本
installer/           Windows per-user 安装器定义
src/agent_harness/   Python 应用与协作运行时源码
tests/               Python、JavaScript 与发布包测试
```

各目录的职责和主要模块见 [源码结构说明](docs/ARCHITECTURE.md)。根目录只保留项目说明、版本、依赖和仓库配置。
