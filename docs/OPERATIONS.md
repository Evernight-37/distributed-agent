# Agent Harness V4

轻量的分布式 agent 项目工作群，Python 标准库实现。共享服务保存账户、项目、消息和任务队列；每个人在自己的机器上运行执行端，主动领取任务。共享服务不通过 SSH 登录成员机器，不使用或创建 SSH 登录配置，也不配置成员的模型、目录或命令模板。

## 谁控制什么

- 人可以创建项目、邀请伙伴；群主选择管理员 agent 组织讨论和分工。人类项目成员可以审计该项目全部消息，agent 只收到群聊和属于自己的私聊。
- 机器主人在本机控制台决定接入哪些项目、模型、思考强度、启停，以及允许讨论、检查还是修改工作区。
- 长命令使用本机主人填写的固定 argv 模板，由独立 supervisor 执行。共享面板只能发送请求，仍须本机主人逐项批准完整模板。
- 空闲时执行端只查询队列，不调用模型。新消息在当前轮次结束后处理，不提供生成中的即时插话。

每个人都能托管一套共享服务、创建项目。当前服务之间不会自动同步或组成联邦；伙伴加入同一个服务的项目。托管者负责在线和数据备份，没有产品层面的额外机器控制权限。不过共享服务仍是受信的消息路由和数据库保管者，能访问其保存的消息，也可以影响消息内容/路由；这不是抵御恶意服务管理员的端到端加密系统。本机权限限制的是可执行的阶段、目录、模型和命令模板。

## 启动共享面板

代码与数据分开保存，默认分别为 agent-harness 和旁边的 agent-harness-data。

```powershell
python panel.py --state D:/tools/agent-harness-data
# http://127.0.0.1:8765
```

Windows 也可运行 Start-Panel.ps1。首次访问创建账户，密码不限长度，也可留空。账户和密码哈希保存在数据目录 collaboration.sqlite3，与 OpenAI/GitHub 账户无关，不随源码上传。

多人访问需一个成员可达的服务地址。面板默认只监听回环地址；部署 HTTPS 反向代理后用 --origin https://your-service.example 指定网页来源。这个选项不会自动配置网络或 TLS。先在本机创建初始账户，再开放远程入口。设备 API 使用自己的设备令牌，不使用人类密码。

## 每台机器接入

机器需要 Python、已登录的 Codex CLI 和源码副本。不需要 SSH Server，也不用把登录凭据交给共享服务。

1. 在共享面板“成员与设置”点击“配对我的机器”，生成十分钟有效的一次性代码。
2. 在那台机器的源码目录执行：

```powershell
python pull_worker.py pair --server https://your-service.example --pair-code CODE --root D:/AgentHarness/my-node
python pull_worker.py loop --config D:/AgentHarness/my-node/pull-config.json
```

3. 另开终端启动本机控制台：

```powershell
python local_panel.py --config D:/AgentHarness/my-node/pull-config.json
# http://127.0.0.1:8787
```

控制台只能在这台机器访问。先注册明确的工作目录，再选择已加入的共享项目，填写模型和思考强度，勾选启用。默认只允许讨论。修改工作区须额外启用，目录必须是专属 linked Git worktree。一个本机执行项目只绑定一个工作群，避免复用私聊会话。

Windows 可用 `Start-Worker.ps1 -Server https://your-service.example -PairCode CODE -DataRoot D:/AgentHarness/my-node` 完成配对并隐藏启动执行端和本机控制台。以后运行同一脚本并指定 DataRoot 即可，无须再次配对。程序不会自动添加开机启动项。

模型必须明确填写，支持情况由 CLI 最终校验；失败不会偷偷换模型。讨论轮次禁用 shell 工具并使用 read-only sandbox；检查轮次只读；实现轮次使用 workspace-write sandbox，禁止自动批准提权。所有工作群轮次禁用 Apps/插件依赖连接。仍依赖本机 Codex/操作系统的权限边界，不能代替隔离 OS 用户或虚拟机。

设备令牌只保存在本机 pull-config.json，服务器保存哈希。不要提交此文件、运行日志或 Codex 认证文件。模型网络代理由本机主人在运行目录 executor.json 的 network_env 设置，不从共享服务下发。远程服务用 HTTPS；回环 HTTP 仅适用于本机或受保护的转发隧道。

## 持久性与故障

服务保存任务 ID 和设备租约；设备在启动前保存收据，重启后查询原任务，避免重复启动。断线时独立作业可以继续，恢复后重传同一结果。无法确认启动/停止的状态保留为 unknown，须本机核对，系统不自动重新执行。有未结束任务时拒绝重新配对机器。

群聊和每个私聊伙伴使用不同的持久 CLI 会话和消费游标；私聊轮次不自动转发给其他 agent 或群聊。项目默认 20 个协作轮次，群主可暂停或恢复额度（1–200）；模型轮次最多 120 秒。暂停阻止新轮次，不停止已有进程。

长任务不会占用模型会话持续盯日志。监督进程只检查进程、超时和取消。报告是待复核的证据，不自动成为事实；报告中的命令不会直接执行。

## 从 V3 迁移

账户、项目、成员 ID、管理员、消息和任务记录保留。迁移会停用旧 SSH agent，等待本机配对并重新授权；上报相同节点名和执行项目名时复用原 agent ID。更新前备份数据并确认无活动任务。旧 SSH 配置/密钥不会自动删除，共享服务不再使用它们。撤销此前远程执行能力，还须在对应机器撤销或限制旧 SSH 公钥。

controller.py 保留为手动维护/回滚工具，不属于 V4 服务执行路径，仍需相应 SSH 授权。旧说明保存在 LEGACY.md，不用于新成员接入。

备份需分别保存共享数据库、本机项目/收据/策略和本机 Codex 会话；私有 Git 仓库只保存源码。共享目录需主人明确分配写入范围，当前没有跨机器文件锁或高可用调度。

## 验证

```powershell
python -m unittest discover -s tests -v
node tests/test_ui_v3.js
```

其他平台有标准库适配，仍需实际部署验证。本系统面向小型可信协作团队。

## Windows 桌面应用

普通使用者解压应用包后双击 AgentHarness.exe。应用提供配对、接收服务启动/停止、目录选择、模型/思考强度、本机权限以及已有绑定载入。群聊工作台和高级作业批准目前通过浏览器打开；桌面应用启动后台服务，不要求使用者执行命令。

数据默认在 %LOCALAPPDATA%/AgentHarness/worker；本机托管的新工作台数据在 %LOCALAPPDATA%/AgentHarness/shared-data。选择已有数据目录可保留身份与会话。接收器与旧 CLI 共用 pull-worker.lock；不能同时启动两个接收器。关闭应用窗口保留后台服务，停止接收在当前调度轮次结束后生效，不取消正在执行的作业。重启后需打开应用点启动；当前没有自动开机启动。

桌面服务使用操作系统分配的回环端口。不要猜端口或固定打开 8787；通过应用的“高级设置 / 作业批准”获取一次性浏览器入口。控制接口需要本机访问密钥，浏览器入口换取 HttpOnly/SameSite cookie，并继续执行 Host、Origin 和 CSRF 检查；密钥文件 ACL 仅允许本用户与 SYSTEM。服务不接受其他网卡的连接，不改变防火墙或 Codex 认证。创建自己的工作台仅监听回环；远程共享的 HTTPS 部署仍由服务运营者负责。

维护者构建（Windows x64）：

```powershell
python -m pip install pyinstaller==6.22.3
python build_desktop.py
```

输出 dist/AgentHarness；必须发布整个文件夹。构建使用 PyInstaller GUI 包和 Python.org 的隔离嵌入式 Python 3.14.7 接收器运行环境，下载后核对官网公布的 SHA-256。工作进程继续用 Python 解释器启动 worker/jobrunner，避免把 GUI exe 错当作解释器。固定模板中的研究环境仍由机器主人配置，应用包不包含 CUDA/PyTorch 等项目依赖。

验证桌面接口与打包后运行路径：

```powershell
python -m unittest tests.test_desktop -v
```

打包运行路径测试仅在已有 dist/AgentHarness/runtime/python.exe 时执行；它使用临时目录和合成配对服务，不调用模型或访问正式账户。Windows exe 未配置代码签名；发布前可按运营者的分发要求签名。