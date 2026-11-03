# Changelog

## 未发布

- 新增应用内安装更新：点“安装并重启”后先重新校验安装包，写入一个位于数据目录（不在安装目录）的启动器，应用优雅退出，启动器等待进程结束后静默运行安装包（`/VERYSILENT`）或替换 macOS 应用包，再重新启动新版本。有正在进行的传输或 Agent 任务时先提示，可以取消。启动后用版本比对确认是否真的生效，不一致时提供“回到上一版本”，回退会重新下载上一个版本并同样静默安装。安装始终需要用户点一次，不会在后台静默替换。
- 新增可配置的版本检查：设置页“应用更新”可选每天、每周、每月或从不，只向本项目 GitHub Releases 查询最新 tag 并与当前版本比较；发现新版时在启动和设置页提示，并可打开下载页，仍由用户手动下载安装。没有网络时只记录一次尝试（一小时内不重试），不下载、不上传任何其它信息。
- 新增后台预下载：发现新版本后可在应用内下载本机对应的安装包（Windows setup / macOS zip / Linux tarball），流式写入并同时计算 SHA-256，与发布时间提供的 digest（或 `.sha256` 资产）比对通过后才落到 `updates/pending/`；校验失败或缺少校验值一律不保留。使用前会再校验一次。安装仍然是独立的一步，需要用户确认，不会在后台替换程序。
- Agent 可把项目目录中的小文件附在当前群聊或私聊回复中；每条最多 5 个，每个最大 5 MB。机器所有者按员工设置“请求批准”“帮我批准”或“完全访问权限”，默认逐件批准；同账户任意已登录桌面可审阅，批准后接收端重新核对文件哈希。
- Windows 读取附件时以已打开句柄核对真实路径和文件属性；Mac/Linux 使用逐级目录文件描述符读取。附件继承原消息的下载权限，任务租约和幂等槽位阻止跨任务附件复用。
- 修复 Windows 上本机执行器收不到任务的问题：`.cmd` 启动器会吞掉重定向的文件句柄，而 worker 之前把 `prompt.txt` 直接作为子进程 stdin，导致子进程读到空内容并立刻失败。npm 安装的 Codex 与 DeepSeek Harness 的 `dsh.cmd` 都受影响，便携包里的 `codex.exe` 不受影响。现在改为管道加喂数据线程，结束后回收句柄。

- 本机执行器不再写死 Codex：设置页新增“本机执行器”，可在这台机器上选择 Codex CLI 或 DeepSeek Harness。执行器按机器保存，同一项目里不同机器可以各用各的，因此 Codex 与 DeepSeek Harness 的 Agent 可以同时待在一个群聊里。
- 新增 DeepSeek Harness 执行链路：以 `dsh --profile headless --patch <本轮补丁> --json -` 一次性运行，任务文本走 stdin。每轮生成 JSON 加载补丁固定模型与 `approval: never`（无人值守时失败关闭），沙箱模式由 `DSH_PERMISSION_MODE` 决定——implement 为 workspace-write，讨论与审阅为 read-only。
- 把 DeepSeek Harness 的 `session`/`status`/`turn_end`/`final` 事件映射为既有的 `thread.started`/`turn.completed`/`item.completed` 契约，worker、broker 与协调器不需要认识第二套方言。会话沿用原来的 `session.json`，通过 `--session-id` 续接。
- DeepSeek Harness 没有常驻 app-server，群聊的托管轮次对它退回一次性进程，不再尝试启动 warm worker；结构化回复只靠提示词契约约束（Codex 仍使用 JSON schema）。
- DeepSeek Harness 的失败码会被翻译成可操作的中文提示（缺 API Key、会话无法续接、权限预设冲突、模型不被识别），原始错误保留在 `provider_error`。
- 修复接收端心跳读错目录的问题：`pull_worker._health` 之前读 `codex-cli.json`，而该文件只写在应用数据目录，接收端目录里从来没有，导致 Codex 组件恒为“尚未配置”；现在改为读接收端自己的 `executor.json`。
- 新增“在 DeepSeek Harness 中使用协作”：DeepSeek Harness 没有 `codex mcp add` 的等价命令，因此把 MCP 服务登记进 **desktop profile** 的补丁层 `~/.dsh/profiles/desktop/cordis.patch.yml`，作用域只到桌面应用里的普通会话——被派发任务用的 headless profile 不读它，无需在每轮补丁里关闭。该文件也是 DSH 配置编辑器的写入目标，编辑器按条目替换并保留其它内容，所以只增删 Harness 标记的两行即可共存；写入保留 `.agent-harness.bak` 备份，写后把该补丁当作 overlay 交给 `--dump-config` 解析并在失败时回滚，profile 不存在则拒绝写入。
- DeepSeek Harness 的 MCP 条目改为指向一个稳定启动器 `mcp/launch-<profile>-dsh.cmd|sh`：运行时装在内容寻址目录里，直接钉住会在应用升级后失效（症状是协作工具悄悄消失）。启动器路径不变，内容在每次打开设置或重新连接时刷新。设置页会显示“条目已过期，请重新连接”，而不是让用户自己发现。
- DeepSeek Harness 的思考强度改为按模型目录投影：`low`/`high` 原样传递，`xhigh`/`max`/`ultra` 按 `high` 发送，`medium` 留空使用模型默认档（提供方的档位通常少于员工设置里的六档）。被拒绝的档位会转成中文提示。
- Linux 接收端新增 `executor status` / `executor set codex|dsh [--model …] [--provider …]` 和 `executor mcp connect|status|disconnect`，与桌面应用的执行器选择和“在 DeepSeek Harness 中使用协作”是同一份配置；`status` 和 `doctor` 的组件文案按当前执行器显示。

## 2.1.0 - 2026-10-28

- 增加独立“文件速传”：同账户机器自动接收，不同用户先确认；文件与项目、Agent 解耦，不设固定总大小上限，并支持分块续传和 SHA-256 校验。
- Windows/macOS 控制台可为同账户 Linux 节点建立默认项目目录，也可将跨用户文件请求批准到指定 Linux；Linux 后台使用设备凭据自动续传，项目目录路径和账户私钥不会经过协作服务。
- 同账户远程机器的接收目录可由账户所有者在任意桌面控制台设置；绝对路径随签名指令短暂经过可信服务，目标机器处理后从服务记录中抹除，传输记录本身不保存路径。
- 工作目录改为按“项目 × 机器”配置，Agent 只保存模型与权限；同一 Agent 在不同项目继续使用独立会话和项目记忆。
- 删除已派遣 Agent 前展示项目、管理员角色和活动任务；确认后撤回全部派遣并保留历史。
- 数据库升级到 schema v4，保存独立传输状态、短期凭据摘要和不含本机路径的工作区就绪状态；现有 v1-v3 数据原地迁移并在升级前自动备份。曾试用未发布项目附件方案的开发数据库会把旧元数据改名为只读退役表保留，不会在改写时销毁。

## 2.0.0 - 2026-10-28

- 增加“账户 → 添加 Linux 服务器”AI 配置入口；只允许选择当前账户已接入的 HTTPS 协作服务，一键复制受限运维提示词。Linux 发布包同步携带 `交给AI安装.md`，并由测试验证提示词命令与当前 CLI 一致。
- 退役旧 Hub 浏览器工作台和 username/password Cookie 登录；数据库升级到 schema v3，仅保留签名账户与设备协议，团队地址改为迁移说明页和最小 `/healthz` 探针。
- 增加可选严格 implement 模式：linked Git worktree、干净基线、HEAD 与 Git 跟踪/普通未跟踪文件门禁；它用于防误操作而非监控 ignored 内容或充当文件系统沙箱。
- 增加加密离线恢复包；恢复时为新电脑生成子设备身份，不把根私钥持久化到目标设备。
- Windows 使用 DPAPI，macOS 使用原生 Keychain，Linux 优先 Secret Service 并安全回退到密码加密文件。
- 增加机器可读字段类型/长度/枚举 schema，并将经典脚本共享面收敛到显式 Desktop namespace。
- 发布方式收敛为 GitHub Releases 手动下载：保留 Windows per-user 安装器、便携 ZIP、Linux 独立包与 macOS ZIP，移除未使用的 Windows 自动更新核心、平台证书、公证、发行清单签名和 SBOM 发布附件。
- 删除未文档化且未被产品调用的本机浏览器控制台，正式 WebView 继续通过受密钥保护的 loopback Desktop API 管理后台接收服务。
- 增加 schema v2 的按账户私有事件游标与 Desktop 独立事件泵；私聊不会推进无权成员的游标，前端固定一秒刷新改为低频兼容降级。兼容期浏览器 SSE 随旧 Web 工作台在 2.0 退役。
- 增加统一 health v1 状态、稳定错误代码和 allowlist-only 安全诊断快照；诊断信息不包含身份、服务地址、代理、路径、消息、任务或日志原文。
- Node/receiver 上报经过白名单清洗的 health v1 心跳，多服务员工卡片显示离线、配置异常和运行异常。
- 扩展 Hub 恢复演练，覆盖迁移失败自动回滚、schema v1 备份恢复升级和服务身份保留。
- 将 Desktop 剩余身份、机器主人和项目动作迁入互斥 action group，并为附件和任务补充边界必填字段契约。
- Windows 新建身份增加当前用户 DPAPI 二次保护；既有 v1 加密档案继续可读并在成功登录时迁移。
- 为 Node 领取任务增加 10 秒有界长轮询，空闲时显著减少请求，同时保持旧 Hub 兼容。
- 建立不可变 Desktop/Node/Hub 动作契约及直接必填字段 schema，并迁出 Hub 项目、
  Desktop 会话/设置/团队服务与 Node 项目/作业状态处理器。
- 开始 1.3 API 边界拆分：Desktop 控制、Node 执行和 Hub 项目入口使用独立适配器与版本化动作契约。
- 将 Desktop 巨型内联页面机械拆为独立 CSS 和按 core/dashboard/project/settings/account/runtime
  分工的脚本，并增加外部资产语法与真实 WebView GUI 验收。
- 将 Desktop 接收端的长轮询切成一秒有界片段，使停止和重启不再被十秒空闲请求拖延。

## 1.2.0

- 引入有版本的 SQLite schema、迁移前自动备份、完整性检查和有限备份保留。
- 在应用的协作服务设置中提供一致备份与确认恢复；恢复前自动生成安全快照。
- 限制匿名会话与待处理关联请求，清理已过期临时状态，并轮转后台日志与本地任务结果。
- Hub、桌面端、MCP 与节点轮询加入显式协议兼容检查和清晰升级提示。
- Linux 接收端改为自包含发布包，不再要求最终用户安装 Python、pip 或 venv。
- Linux CI 和 tag 发布同时生成 x64 与 ARM64 原生包。
- Linux 并排升级同时支持旧 Python 包和新独立运行包，并保留自动回滚。
- Windows 与 Linux 发行产物改由统一 tag 流水线生成，并附机器可读的完整性清单。
- 发布流水线为各平台产物生成 SPDX SBOM，并在拉取请求中审计 Python 与 JavaScript 依赖。
- 将存在公开安全公告的 cryptography 46.0.3 升级到 50.0.2。
- Python 源码、测试和便携运行时统一使用标准 `agent_harness` 包；移除测试路径注入，并增加 wheel、净室和已打包接收端验证。

## 1.1.1

- 修复 Linux 连续升级、失败重试、代理热加载、CLI 路径更新、日志与健康检查。
- 默认拒绝以 root 运行 Linux 接收端。
