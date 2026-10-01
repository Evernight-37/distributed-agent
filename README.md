# Agent Harness

轻量的分布式 agent 项目工作群。Python 标准库实现，不要求特定 GPU、机器名、Tailscale 或同一局域网。V3 提供人类账户、群主/管理员 agent、群聊与私聊审计、机器所有权、持久会话和独立后台作业。当前执行器为 Codex CLI，传输为 SSH 或本机进程；其他执行器需要适配生命周期接口。

## 目录与启动

代码与运行数据必须分离。默认数据目录是代码目录的同级 `agent-harness-data`；可用 `--state` 或 `AGENT_HARNESS_STATE` 指定。不要把运行数据放进代码仓库。

```powershell
python panel.py
# 打开 http://127.0.0.1:8765
python controller.py list
```

首次打开面板创建自己的账户和密码（至少 12 字符），无需把密码交给 agent。初始账户继承已有节点和执行项目，后续用户凭群主的一次性项目邀请注册/加入。左栏列出自己的项目，主区为聊天、任务、成员设置。人类项目成员能审计该项目全部私聊；agent 只能获得群消息及属于自己的私聊。模型、思考强度、启停、取消和命令模板只能由对应机器/agent 的人类所有者控制，群主和管理员 agent 无权越过这个限制。

面板仅监听 `127.0.0.1`；检查 Host、Origin、会话 cookie 和每会话 CSRF，密码用 scrypt 哈希保存。多个人异地访问时需自行部署 HTTPS 反向代理，并用 `python panel.py --origin https://your-internal-domain.example` 指定实际来源。这个选项不会配置网络、TLS 或开放端口。首次创建账户前不要开放代理；先由服务器所有者在本机初始化。应用账户权限不是对恶意本机管理员的隔离。

### 工作群如何运行

群主选一个管理员 agent。群聊新消息唤醒管理员；定向私聊唤醒收件 agent。管理员能分派明确契约的任务，普通成员能报告进度、提出异议和发送消息。每个 agent 同时仅有一个模型轮次；任务期间新消息保留在独立收件队列，轮次结束后处理。群聊和每个私聊伙伴使用不同的持久 Codex 会话及消费游标，避免把私聊历史带进群聊轮次。私聊轮次不允许自动转发给其他 agent 或群聊；管理员在私聊中要公开讨论新的安排，需人类在群聊发起该讨论。

模型输出必须符合结构化 JSON 协议。模型消息不会被解释成 shell 命令。失败/取消通知不自动触发重试；后台作业完成才会唤醒一次结果处理。每个项目默认最多 20 个协作轮次，群主可暂停或恢复额度（1–200）。暂停阻止新轮次，不会杀掉已经运行的作业。模型轮次最多 120 秒，超过后停止并等待人工处理，不会静默换模型。

手动派任务/取消由该 agent 所有者操作；其他项目成员可以在群聊或私聊提出需求，由管理员按已接入的协作权限协调。工作群轮次禁用 CLI 的 Apps/插件依赖自动连接，避免把账号连接器带入默认工作会话。项目文件规则仍由 CLI 加载。节点若需要代理，由节点运营者在仓库外 executor.json 设置 `network_env` 的 HTTP_PROXY/HTTPS_PROXY/WS_PROXY/WSS_PROXY 等允许字段；只影响这个 worker 的子进程，不改系统网络或账户认证。

Windows 控制器可选反向代理：节点连接 JSON 增加 `"reverse_proxy":{"local_port":7897,"remote_port":17897}`，远端 worker 的 network_env 使用 `http://127.0.0.1:17897`。控制器会维护仅绑定远端回环地址的 SSH `-R` 隧道；本机必须已经有对应代理。它不会启动代理软件，不允许远端局域网直接访问该端口，不改变 sshd、防火墙或系统代理。隧道依赖控制器联网，节点有自己可用网络时不必启用。隧道 PID/进程启动标识保存在数据目录 tunnels/，只核对相同进程后才能手动停止。节点模型仍使用节点自己的登录凭据，代理通道不复制认证文件。

长命令由独立 Python supervisor 执行，不占用模型会话。agent 只能请求模板名，机器所有者在“后台作业”里批准；命令模板在成员设置的高级区由所有者明确填写固定 argv，执行不经过 shell。模板批准相当于批准其中的完整程序权限；不要批准读取凭据、私有会话目录或打印秘密的模板。监督进程会检查超时/取消，这种轻量的进程管理不调用模型。调度器在空闲时不调用模型；运行中的 RPC 使用有限线程并行处理，离线节点不会独占整个服务。

重启调度器时，启动结果未确定的模型任务标记为 unknown 并查询已有 job ID；不会重复提交。若远端没有对应任务，需本机运营者核对后处理该记录。配置结果未知时显示 configuration_pending，所有者重新确认设置后才能派新任务。程序不提供多租户 OS 隔离、跨节点工作区锁或高可用调度；不互信的合作方应使用各自 OS 用户/隔离环境，且只能接入明确授权的工作区。

这里的自动唤醒仅针对 harness 管理的 CLI 会话，不会唤醒当前 Codex 桌面聊天，也不提供正在生成中的即时插话。

## 注册工作节点

每个节点需要 Python、已登录可用的 Codex CLI，以及验证过的 SSH 公钥认证。SSH 地址可以是 IP 或 DNS 名，来源不限于 Tailscale。把节点配置放在仓库外，例如 `../agent-harness-data/new-node.json`：

```json
{
  "host": "worker.example.org",
  "user": "worker",
  "port": 22,
  "platform": "windows",
  "identity_file": "D:/private/worker-key",
  "known_hosts_file": "D:/private/verified-known-hosts",
  "remote_python": "D:/miniconda3/python.exe",
  "remote_root": "D:/AgentHarness/node-data"
}
```

```powershell
python controller.py --node worker-a init --connection ../agent-harness-data/new-node.json
python controller.py --node worker-a probe
python controller.py --node worker-a register project-a --local-path D:/projects/project-a --remote-path D:/worktrees/project-a-peer --model YOUR_MODEL --effort medium --timeout 1800
python group_cli.py bind-node --username YOUR_PANEL_USERNAME --node worker-a
```

`init` 部署 worker 源码，保留远端项目/会话；有活动或尚未核对的任务时拒绝部署。Windows 以隐藏 WMI 进程运行 supervisor，已实机验证 SSH 断开后继续工作。Windows 自动寻找桌面安装的 Codex。也可在远端 root 的 `executor.json` 写入 `{"cli_path":"绝对可执行文件路径"}`；该文件不进入仓库。

节点归属由受信服务器运营者通过上述 CLI 指定，网页用户不能抢占未分配的连接。人类所有者随后在项目“成员与设置”中选自己节点、填写已注册的执行项目名并接入 agent。一个执行项目只能对应一个 agent，不能跨项目复用私聊会话。一个项目可接多个机器、多个所有者的 agent。诊断项目不代表实际研究仓库；实现任务必须另注册真实项目的专属 linked Git worktree。

本机节点连接配置使用 `{"transport":"local","platform":"windows","remote_python":"D:/miniconda3/python.exe","remote_root":"D:/AgentHarness/local-node"}`，同样通过 `controller.py --node local init --connection ...` 部署。数据目录和工作区仍须与源码分离。

POSIX SSH 适配器可配置 `platform: posix`、`remote_python: python3` 和绝对 `remote_root`，从 PATH 查找 Codex，使用独立会话/进程组。它尚未在真实 Linux/macOS SSH 服务上验证；部分系统会清理注销用户的进程，需要由服务管理器托管 supervisor。不要把 Windows 的实机验证视为 POSIX 验证。

## 项目设置与持久会话

项目绑定固定节点。项目名在控制器内唯一。任务按项目自动路由，显式指定错误节点会被拒绝。多个节点可同时执行各自的任务；单个节点的相同/嵌套目录不会同时执行。

```powershell
python controller.py configure project-a --model YOUR_MODEL --effort high --timeout 3600
```

设置只在空闲时修改，对下一轮生效，保留原 thread ID。模型名称可填写；思考强度选项由 harness 校验语法，是否为该账户/模型支持由 Codex CLI 最终校验，失败会显示错误。不会自动改用便宜模型。配置传输结果不明时保留 `configuration_pending`，下次使用项目时从远端权威配置恢复。

会话记录由节点上的 Codex 自己保存；共享记忆、会话 ID、任务和审查记录保存于数据目录。Git 代码备份不包含这些运行状态，换机器恢复还需要分别安全备份数据目录及节点的 Codex 会话。复制数据目录前停止提交并确认无活动任务；同一份控制器状态不要被多个控制器同时拷贝后分别使用。

## 合作协议与边界

每项任务包含目标、允许文件/系统、禁止修改、验收标准、验证方法和返回格式。远端应提出异议并提供证据。结果进入 `needs_review` 后仍需主控独立验证；报告中的命令不会自动执行，报告不会自动成为共享事实。实现任务必须使用预先注册的专属 linked Git worktree。

控制器通过 `submit/status/fetch/cancel/project/memory/configure` RPC 与节点通信。传输层当前提供 SSH，执行器当前提供 Codex exec。要接入其他 agent，需适配这些生命周期操作、稳定会话标识、进度事件与结果格式；仅网络可达还不够。可以复用契约/记忆/审查层，并替换传输与执行器。

直接使用 V2 CLI 的普通消息仍排队到下一次手动提交；V3 工作群的消息由独立调度器触发下一轮。取消会终止任务进程，不能视为优雅暂停。实时 steering 需要另接支持中途输入的执行器；桌面聊天唤醒需要独立集成。

不同节点如共享同一网络文件系统，需由使用者分配互不重叠的写入范围；当前没有跨节点文件锁或共识协议。这个控制器服务小型可信团队，尚不具备多租户隔离、调度高可用与任意网络传输适配。

## 验证与版本管理

```powershell
python -m unittest discover -s tests -v
node tests/test_ui_v3.js
```

只提交源码、网页、测试和文档。禁止提交私钥、凭据、节点配置、项目路径、会话、任务日志或数据目录。 `.gitignore` 是辅助保护，提交前仍应检查 `git diff --cached`。兼容部署逐文件原子替换；不兼容更新需要停用所有调用方并执行明确的数据迁移。

V3 新增 collaboration.sqlite3 保存账户、群成员、消息、轮次和审批。代码 Git 仓库不包含数据库；安全备份运行数据时应停止面板和提交并确认后台作业结束，保留数据库及其 WAL 文件一致性。旧项目 JSON 与 Codex 会话保留，初次账户迁移不发起模型请求。应用协议只过滤提交给模型的上下文；具备同一 OS 用户权限的模型工具或恶意管理员仍可绕过它，因此节点必须受信。
