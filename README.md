# Agent Harness

轻量的分布式 Codex 协作控制器。Python 标准库实现，不要求特定 GPU、机器名、Tailscale 或同一局域网。控制器通过 SSH 连接工作节点，保存项目会话、任务契约、进度、带来源的记忆与独立审查记录。

## 目录与启动

代码与运行数据必须分离。默认数据目录是代码目录的同级 `agent-harness-data`；可用 `--state` 或 `AGENT_HARNESS_STATE` 指定。不要把运行数据放进代码仓库。

```powershell
python panel.py
# 打开 http://127.0.0.1:8765
python controller.py list
```

控制面板提供节点探测、项目注册、任务提交/查看/取消、下一轮消息、共享记忆、模型和思考强度设置、独立审查。仅监听本机 `127.0.0.1`。入口是本机同一用户的管理工具，不构成对恶意本机进程的安全边界。禁止把它直接转发到公网。网页请求检查 Host、Origin 和随机 CSRF token，无外部脚本。

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
```

`init` 部署 worker 源码，保留远端项目/会话；有活动或尚未核对的任务时拒绝部署。Windows 以隐藏 WMI 进程运行 supervisor，已实机验证 SSH 断开后继续工作。Windows 自动寻找桌面安装的 Codex。也可在远端 root 的 `executor.json` 写入 `{"cli_path":"绝对可执行文件路径"}`；该文件不进入仓库。

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

普通消息排队到下一次提交的任务中，不会进入正在执行的那一轮。取消会终止任务进程，不能视为优雅暂停。没有后台自动互聊、实时双向插话或自动唤醒 Codex 桌面聊天。实时 steering 需要另接支持中途输入的执行器（例如 Codex App Server），桌面聊天唤醒需要独立通知/调度集成。

不同节点如共享同一网络文件系统，需由使用者分配互不重叠的写入范围；当前没有跨节点文件锁或共识协议。这个控制器服务小型可信团队，尚不具备多租户隔离、调度高可用与任意网络传输适配。

## 验证与版本管理

```powershell
python -m unittest discover -s tests -v
```

只提交源码、网页、测试和文档。禁止提交私钥、凭据、节点配置、项目路径、会话、任务日志或数据目录。 `.gitignore` 是辅助保护，提交前仍应检查 `git diff --cached`。兼容部署逐文件原子替换；不兼容更新需要停用所有调用方并执行明确的数据迁移。
