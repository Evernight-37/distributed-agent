# Foolship

Foolship 是给小型可信团队用的分布式 Agent 协作应用。每个人在自己电脑上管理 Codex 或 DeepSeek Harness、模型、工作目录和执行权限；项目服务只负责成员、会话、共享记忆与任务路由。文件在设备之间直连，服务端不存字节。

## 一则寓言

『智识以已知丈量寰宇，却唯有未知，能为其疆界赋予意义。倘若最后一道命题终获证明，试问，那将是真理的凯旋，还是求知的葬礼？』

故有愚人集于一舟，血肉与机巧各执罗盘，向星图未载之境远航。

『哎呀，何必说得那般庄严？咱们不过是想给那颗机器头，讲个祂还没听过的笑话罢了！』

---

“Erudition charts the cosmos by what is known, yet only the unknown lends meaning to its horizons. Should the final proposition ever be proven, would it herald the triumph of truth—or the death of inquiry?”

Thus the fools gathered aboard a single ship, some of flesh, others of clockwork, each bearing a compass of their own, bound for shores no star chart had named.

“Oh, must you make it sound so grand? We’re only sailing off to tell that big metal head a joke it hasn’t heard before!”

## 安装

当前稳定版为 **2.10.2**，有两种装法：

- **自己装**：从 [GitHub Releases](https://github.com/Evernight-37/distributed-agent/releases/latest) 下载与系统和 CPU 架构相符的发布包。
- **让那台机器上的 AI 装**：把对应的任务书交给它，由它下载、校验、安装（见下）。

| 使用环境 | 下载文件 | 使用方法 |
| --- | --- | --- |
| Windows x64（推荐） | `Foolship-2.10.2-windows-x64-setup.exe` | 双击按向导安装，从开始菜单启动 |
| Windows x64（便携） | `Foolship-2.10.2-windows-x64.zip` | 完整解压到独立目录，运行其中的 `Foolship.exe`；不要只复制 EXE |
| macOS Apple Silicon | `Foolship-2.10.2-macos-arm64.zip` | 解压后把 `Foolship.app` 放进“应用程序”打开；未做 Apple 签名，系统可能要求在隐私与安全性中确认 |
| Linux x64 / ARM64 | `Foolship-2.10.2-linux-x64.tar.gz` 或 `Foolship-2.10.2-linux-arm64.tar.gz` | 无界面接收端，见 [Linux 说明](docs/LINUX.md) |

发布包自带运行时，不需要另行安装 Python、pip 或 venv。Windows 需要 Edge WebView2；每台实际执行 Agent 的机器仍需安装并登录 Codex 或 DeepSeek Harness。升级只替换程序文件，不动账户数据。

### 让那台机器上的 AI 装

仓库为三种目标各准备了一份任务书，交给那台机器上的 AI（Codex 或 DeepSeek Harness）执行即可：

| 目标机器 | 任务书 |
| --- | --- |
| Linux 服务器（无界面接收端） | [`assets/web/linux-ai-setup.md`](assets/web/linux-ai-setup.md) |
| Windows 电脑 | [`assets/web/windows-ai-setup.md`](assets/web/windows-ai-setup.md) |
| macOS 电脑 | [`assets/web/macos-ai-setup.md`](assets/web/macos-ai-setup.md) |

任务书里写明了下载哪个包、如何校验、装到哪里、哪些步骤必须停下来等人确认、装完如何报告。唯一需要补充的是团队服务地址：Linux 接收端在最后一步要把机器关联到已有的团队服务，所以那时必须有；桌面应用装完由你登录，团队服务可以之后再建。

无需克隆仓库即可取用：

```
https://raw.githubusercontent.com/Evernight-37/distributed-agent/main/assets/web/linux-ai-setup.md
https://raw.githubusercontent.com/Evernight-37/distributed-agent/main/assets/web/windows-ai-setup.md
https://raw.githubusercontent.com/Evernight-37/distributed-agent/main/assets/web/macos-ai-setup.md
```

已经有一台装好并登录的机器时，也可以让应用直接生成这份文本：**账户 → 在另一台电脑上安装** 选择目标机器后会替换好地址。完整说明见 [AI 配置流程](docs/AI-SETUP.md)。

## 第一次使用

1. 启动应用，创建账户；已有账户可以导入离线恢复包。
2. **设置 → 协作服务 → 创建团队入口**，得到 `https://….ts.net` 形式的团队地址。
3. 其他人在应用里用这个地址和项目邀请码加入；不需要为每个团队新建账户。
4. 接入自己的机器与 Agent，派发任务；群主指定管理员 Agent。

工作目录属于“项目 × 机器”，不属于 Agent：第一次向项目派遣本机 Agent 时选择，同机同项目的多个 Agent 共享该目录，但会话和项目记忆仍按项目隔离。

按钮级流程见 [新手教程](docs/QUICKSTART.md)。单机使用可以跳过 Tailscale。

## 多台机器

一个账户可以关联多台机器——自己的另一台电脑、自己的服务器。每台都用上面任一方式安装，登录同一账户即可：同一账户的机器之间传文件不需要逐次确认，Agent 任务也可以派给任意一台。

## 模型与执行器

桌面端按**模型**决定用哪个 CLI：**设置 → 本机可用模型** 里维护一张表，写明这台电脑能跑哪些模型、每个模型由 Codex 还是 DeepSeek Harness 执行。员工的模型必须从这张表里选，派发时按模型自动选执行器——所以同一台机器可以同时跑两种，不存在“默认执行器”。Linux 无界面接收端另有机器级的 `foolship executor set codex|dsh`，用来指定它默认用哪个 CLI。

| 对比项 | Codex CLI | DeepSeek Harness |
| --- | --- | --- |
| 每轮命令 | `codex exec --json` | `dsh --profile headless --patch <本轮补丁> --json -` |
| 权限 | `--sandbox read-only/workspace-write` | `DSH_PERMISSION_MODE`，同一套沙箱语义 |
| 会话续接 | `resume <thread-id>` | `--session-id <session-id>` |
| 群聊托管轮次 | 常驻 app-server，低延迟复用 | 每轮一次性进程，无 app-server |
| 结构化回复 | JSON schema 强约束 | 仅提示词契约 |

想在这台机器的普通对话里使用协作功能，两个执行器各有一个开关：

- **DeepSeek Harness**：**设置 → 在 DeepSeek Harness 中使用协作**。它只写 `~/.dsh/profiles/desktop/cordis.patch.yml`，被派发任务用的 headless profile 不受影响；写入前备份，解析失败回滚，细节见[架构说明](docs/ARCHITECTURE.md)。
- **Codex**：**设置 → 在 Codex 中使用协作**。走本机 stdio MCP，不开放额外端口；委托身份不能邀请成员、改管理员、控制别人的机器或批准命令。

两处都需要在**新会话**里才能看到这些工具。

## 文件速传

侧边栏的 **文件速传** 不设固定大小上限，服务端不存任何字节：发送方在自己机器的 Tailscale 地址上开一个临时端口，接收方按需拉取，协作服务只转交“谁发给谁、大小、地址”这类信令，Tailscale 的 WireGuard 已提供端到端加密。传输按 Range 续传，完成后校验大小、修改时间与 SHA-256；发送方在续传期间改过文件会重新下载，而不是拼接出错误内容。

接收规则：同一账户的机器自动接收，落到目标机预设的接收目录；不同用户第一次来件需要接收方确认，之后记住该发送账户。桌面端和 Linux 无界面端都能接收，应用运行期间在后台进行。前提是双方都安装并登录 Tailscale、且两台机器能互相访问；发送方首次监听端口时 Windows 会弹出防火墙授权。

传输不排队：发送方不在线就没有可拉取的来源；接收端离线期间停在服务端的传输，重新上线时只有发送方仍在监听才能继续。Linux 接收端默认落到 `received-files`，也可以在桌面端为远程机器指定接收目录；Linux 端不提供主动发送的 CLI。

聊天里的项目小附件不走这条通道，每个最大 5 MB，按消息保存。

## 安全边界

- 项目没有中心云、遥测或维护者账户服务；账户、项目和消息只保存在参与者的机器和项目选定的协作服务上。
- 协作服务的机器主人能读取其数据库中的项目内容，因此成员应信任托管者。
- 应用不安装组网软件、不替你开放防火墙，也不接管 Codex 或 GitHub 凭据。
- 身份先用本地解锁密码加密，再优先交给系统安全存储（DPAPI / Keychain / Secret Service），不可用时退回 Scrypt + AES-GCM 文件；密钥不上传。
- 根设备可以导出离线恢复包；导入时只在内存中解密，并重新生成子设备身份。恢复包不含服务地址、项目数据或消息。
- Agent 在回复中附项目文件默认逐件批准，可改为“帮我批准”或“完全访问权限”；这只影响附件发送，不改变执行授权。详见[附件说明](docs/AGENT-ATTACHMENTS.md)。
- 实现任务可开启“安全模式”，要求干净的 linked worktree 且只改 `allowed_files`；这是防误操作措施，不是文件系统沙箱。

## 数据、备份与更新

安装器是 per-user 的，默认装在 `%LOCALAPPDATA%\Foolship\app`；便携包解压到任意可写目录即可。程序、数据 `data\` 和后台运行时 `service-runtime\` 都在安装目录内，可以整体备份；升级只替换程序文件，不写入也不删除 `data\`。macOS 例外：`.app` 是封签目录，运行时和数据放在 `~/Library/Application Support/Foolship`（数据在其中的 `data/`）。

需要把数据放到别处时，设置环境变量 `FOOLSHIP_STATE`。注意数据跟着安装目录走，手动删除应用文件夹会一并删除数据。

托管者可在 **设置 → 协作服务** 备份或恢复项目数据库：备份用 SQLite 一致快照，恢复只接受完整性检查通过的数据库，并在替换前自动保存当前数据库。版本检查可以设为每天/每周/每月/从不，只查询本仓库的 Releases；下载后按发布页的 SHA-256 校验，安装始终需要点一次确认。二进制未签名，首次安装时系统可能提示未知发布者。

## 从源码构建

```powershell
python -m pip install -r requirements-dev.txt
npm ci
ruff check src scripts tests
ruff format --check src scripts tests
python -m unittest discover -s tests -t . -v
npm run format:check
npm test
```

`requirements-dev.txt` 以 editable 模式安装当前 `agent_harness` 包，测试不依赖手工设置 `PYTHONPATH`。构建 Windows 便携包用 `python -m pip install -r requirements-build.txt` 和 `python scripts/build_desktop.py`，结果在 `dist/Foolship`；发布前还应跑 `python tests/cleanroom_release.py dist/Foolship build/cleanroom`。目录职责与模块划分见[架构说明](docs/ARCHITECTURE.md)。
