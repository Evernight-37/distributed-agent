# Agent Harness

Agent Harness 是面向小型可信团队的分布式 Agent 协作应用。每个人管理自己电脑上的 Codex 或 DeepSeek Harness、模型、目录和执行权限；项目服务负责成员、会话、共享记忆与任务路由。

## 使用应用

当前稳定版为 **2.1.0**。从 [GitHub Releases](https://github.com/Evernight-37/distributed-agent/releases/latest) 下载与系统和 CPU 架构相符的完整发布包。

| 使用环境 | 下载文件 | 使用方法 |
| --- | --- | --- |
| Windows x64（推荐安装） | `AgentHarness-2.1.0-windows-x64-setup.exe` | 双击并按向导安装，然后从开始菜单启动 Agent Harness |
| Windows x64（便携） | `AgentHarness-2.1.0-windows-x64.zip` | 完整解压到独立目录，双击其中的 `AgentHarness.exe`；不要只复制 EXE |
| macOS Apple Silicon | `AgentHarness-2.1.0-macos-arm64.zip` | 解压后把 `AgentHarness.app` 放入“应用程序”并打开；本项目未做 Apple 签名或公证，系统可能要求在隐私与安全性中确认打开 |
| Linux x64 / ARM64 服务器 | `AgentHarness-2.1.0-linux-x64.tar.gz` 或 `AgentHarness-2.1.0-linux-arm64.tar.gz` | 使用无界面接收端；优先把包内的 `交给AI安装.md` 交给服务器 Codex，或阅读 [Linux 说明](docs/LINUX.md) |

发布包自带 Harness 所需运行时，不要求另外安装 Python、pip 或 venv。Windows 系统需要 Edge WebView2；每台实际执行 Agent 的机器仍需安装并登录 Codex 或 DeepSeek Harness。升级程序不会自动迁移或删除项目工作目录，账户数据保存在应用目录之外；退出旧版后安装或解压新版即可继续使用原账户数据。Windows 便携版应保留完整目录，Linux 端使用并排升级和回滚机制。

第一次使用请先阅读 [按钮式新手教程](docs/QUICKSTART.md)。教程从 Tailscale 准备开始，分别列出群主、服务托管者和受邀同事应点击的按钮，并在每一步说明成功标志与常见错误。单机使用可以跳过 Tailscale。多人协作的主流程是：

1. 群主从 Tailscale 管理台邀请外部用户，双方确认设备在线。
2. 每个人分别创建自己的 Harness 账户；不要共享账户或设备关联码。
3. 群主在应用里创建团队入口，并分享 HTTPS 团队地址和项目邀请码。
4. 同事凭两项信息加入项目，再把自己的机器和 Agent 接入该服务。
5. 每个人配置和派遣自己的 Agent；群主选择管理员 Agent。

Linux 服务器不要求用户手工照抄安装命令：桌面应用 **账户 → 添加 Linux 服务器** 可以生成带团队地址的安全配置提示词，直接粘贴给服务器 Codex。Linux 发布包也自带 `交给AI安装.md`；详见 [AI 配置流程](docs/AI-SETUP-LINUX.md)。

同一账户可以关联多台电脑。Windows 数据位于 `%LOCALAPPDATA%\AgentHarness\data`，macOS 数据位于 `~/Library/Application Support/AgentHarness/data`；项目工作目录、Codex 登录、设备私钥和研究环境不会自动同步。

## 本机执行器：Codex 或 DeepSeek Harness

打开 **设置 → 本机执行器** 可以在这台电脑上二选一。执行器按机器保存，同一项目里不同机器可以用不同执行器，因此 Codex 和 DeepSeek Harness 的 Agent 可以同时待在一个群聊里，由同一个项目记忆和任务路由协调。

| 对比项 | Codex CLI | DeepSeek Harness |
| --- | --- | --- |
| 每轮命令 | `codex exec --json` | `dsh --profile headless --patch <本轮补丁> --json -` |
| 权限 | `--sandbox read-only/workspace-write` | `DSH_PERMISSION_MODE` 环境变量，同一套沙箱语义 |
| 会话续接 | `resume <thread-id>` | `--session-id <session-id>` |
| 群聊托管轮次 | 常驻 app-server，低延迟复用 | 每轮一次性进程；无 app-server |
| 结构化回复 | JSON schema 强约束 | 仅提示词契约 |
| 思考强度 | 生效 | 生效，但按模型校验：`medium` 用模型默认档，`xhigh`/`max`/`ultra` 按 `high` 发送 |
| 登录 | `codex login` | `DEEPSEEK_API_KEY` 环境变量，或在 DeepSeek Harness 的 Models 页面保存密钥 |

DeepSeek Harness 的模型与模型服务在 **设置 → 本机执行器** 里指定；留空时使用员工上设置的模型。思考强度会传给 DeepSeek Harness，但它按模型自己的目录校验（例如 `deepseek-flash` 只接受 `off`/`low`/`high`），所以 `medium` 留空表示用模型默认档，`xhigh`/`max`/`ultra` 按 `high` 发送。Harness 不读取、不保存执行器的密钥：Codex 的登录状态通过 `codex login status` 查询，DeepSeek Harness 的密钥由它自己的凭据服务或 `DEEPSEEK_API_KEY` 提供。

## 在 DeepSeek Harness 中使用协作

执行器选为 DeepSeek Harness 时，设置页会出现 **在 DeepSeek Harness 中使用协作**，点击“连接到 DeepSeek Harness”即可。

Codex 有 `codex mcp add` 这条官方配置接口，DeepSeek Harness 没有等价命令。它把每个 profile 的配置叠加在一条用户补丁层上，所以 Harness 写的是 **desktop 这一个 profile 的补丁层**：`~/.dsh/profiles/desktop/cordis.patch.yml`。作用域就是你在桌面应用里的普通对话——被派发任务用的 `headless` profile 完全不读它，因此这更接近“装了一个插件”，而不是改动整个 DSH。

DeepSeek Harness 自己的配置编辑器也写这个文件，它按条目替换、并保留其它条目与注释，所以两个写入方可以共存。写入仍然是受控的：

- 只增删 Harness 自己标记的两行（一行注释、一行 JSON 条目），其它内容逐字节保留；
- 条目指向一个**稳定启动器** `mcp/launch-<profile>-dsh.cmd|sh`：Harness 的运行时装在内容寻址目录里，直接钉住运行时的路径会在应用升级后失效，而启动器路径不变、内容每次打开设置或重新连接时刷新；
- 写入前保留 `cordis.patch.yml.agent-harness.bak`，断开连接会把文件恢复原样；
- 写后把这个补丁作为 overlay 交给 `dsh --profile headless --patch … --dump-config` 解析一次，解析失败就自动回滚；
- 如果该文件不是顶层块状列表（例如整体是 flow 风格），Harness 拒绝改写并给出可粘贴的片段；
- profile 不存在时直接报错，绝不自己造一个残缺的 profile。

设置页会区分“已启用”和“条目已过期”：后者说明条目指向的启动命令和当前预期不一致（升级后常见），点一次“重新连接”即可刷新。新的 DSH 会话（或重启桌面应用后）才会看到这些工具。被派发的 Agent 任务不受影响：它们跑的是 headless profile。

## 在 Codex 中协作

登录应用后，打开 **设置 → 在 Codex 中使用协作**，点击“连接到 Codex”。此后可以在普通 Codex 对话中列出 Harness 项目、读取有权查看的会话、发送消息，以及给自己名下的 Agent 派发异步任务。

该入口使用本机 stdio MCP，不开放额外端口。Codex 获得受限委托身份，不能邀请成员、修改管理员、控制别人的机器或批准执行命令。

## 多人和多机连接

项目数据保存在创建项目时选择的协作服务上。托管电脑可在 **设置 → 协作服务 → 创建团队入口** 中建立 Tailscale 私有 HTTPS 地址。同事加入同一 Tailscale 网络后，用该地址和项目邀请码加入项目；不需要为每个团队重新创建账户。Tailscale 显示的 `100.x.x.x` 是机器地址，不是团队服务地址；应用中可分享的地址形如 `https://电脑名.xxxx.ts.net`。

团队地址是 Desktop 与设备接收端使用的服务入口，不再提供浏览器登录工作台。直接在浏览器打开时只显示迁移说明；人类操作请使用 Agent Harness 应用，Codex 通过本机 MCP 协作。

项目没有中心云、遥测或维护者账户服务。账户、项目和消息只保存在参与者自己的电脑及项目选定的协作服务上；应用不会安装组网软件、开放防火墙或接管 Codex/GitHub 凭据。协作服务的机器主人能够读取其数据库中的项目内容，因此成员应信任托管者。

侧边栏的 **文件速传** 是独立于项目和 Agent 的机器间大文件通道，不设置固定文件大小上限。它会分块续传并验证 SHA-256；实际可传大小取决于源文件系统、接收磁盘和临时中转服务的可用空间。同一账户的机器使用目标机预设目录自动接收；不同用户之间先发送请求，由接收者选择保存位置并确认。双方需共同接入至少一个协作服务，文件只在该服务临时中转，接收完成即清除。中转目前不是端到端加密，服务托管者可读取传输中的明文文件，因此仍应选择双方信任的托管者。聊天中的项目小附件仍按消息保存，每个最大 5 MB，不走这套确认流程。

Agent 也可以在回复中附上当前项目工作目录中的小文件。机器所有者可在员工设置中选“请求批准”“帮我批准”或“完全访问权限”；默认逐件批准，批准请求会显示在同一账户任意已登录机器的控制台。权限只作用于项目内附件发送，不改变 Codex 的执行授权。详见 [Agent 回复附件](docs/AGENT-ATTACHMENTS.md)。

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

运行数据、令牌、密钥、日志、构建缓存和发布包均由 `.gitignore` 排除。发布流水线只负责生成并上传 Windows per-user 安装器、Windows 便携包、Linux x64/ARM64 包和 macOS ZIP；项目不包含平台证书、公证或代码签名。

应用内提供可配置的版本检查：设置 → 应用更新 可选每天/每周/每月/从不，只向本仓库的 GitHub Releases 查询最新 tag；发现新版本后可在后台预先把本机对应的安装包下载并校验，安装时先退出应用、替换程序目录、再自动重启。**安装始终需要用户点一次确认**，不会在后台静默替换。安装包完整性用发布页提供的 SHA-256 校验（HTTPS 加发布校验值，防传输损坏与替换，不防"发布者本人"）；项目不持有更新签名密钥，因此不提供无提示自动安装。二进制文件未签名，首次安装时系统可能提示未知发布者。持久数据保存在应用目录之外，升级和回退都不会触碰它。

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
