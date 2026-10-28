# 源码结构

Agent Harness 使用标准 Python `src` 布局，把可运行源码与界面资源、测试和维护脚本分开。源码和测试统一通过 `agent_harness.*` 导入，不修改 `sys.path`。`pyproject.toml` 以根目录 `VERSION` 为唯一版本来源，并允许维护者构建普通 wheel；最终用户仍使用自带运行时的便携包，不需要安装 Python。

## 顶层目录

| 目录 | 职责 |
|---|---|
| `src/agent_harness/` | 桌面应用、账户身份、项目协作、执行端和 MCP 源码 |
| `assets/web/` | 桌面 WebView 和团队服务使用的 HTML、CSS、JavaScript |
| `scripts/` | Windows、macOS、Linux 构建和发行打包脚本 |
| `tests/` | 单元、集成、界面行为和干净安装验证 |
| `docs/` | 新手教程与维护文档 |
| `.github/workflows/` | 按版本标签构建 GitHub Release |

Desktop WebView 入口为 `assets/web/app.html`。样式位于 `assets/web/styles/app.css`，脚本按加载顺序
拆分在 `assets/web/app/`：`core`、`dashboard`、`project`、`settings-components`、
`account-settings` 和 `runtime`。当前仍使用经典脚本以兼容 Windows WebView2 与 macOS WKWebView
的本地文件加载；在状态 namespace 完成前不得改为依赖 `file://` CORS 行为的 ES Modules。

## Python 模块职责

`src/agent_harness/` 内的模块按运行边界命名：

- `production_app.py`、`app_bridge.py`：桌面窗口与受限本机 API。
- `account_*`、`owner_*`：账户身份、设备归属与机器主人的授权边界。
- `collaboration_store.py`、`broker.py`、`coordinator.py`、`project_memory.py`：项目数据、消息、任务路由与项目记忆。
- `file_transfers.py`、`file_transfer_client.py`：独立于项目的账户/设备传输状态机，以及不经过 JSON/Base64 的原生分块传输。
- `desktop_*`、`pull_worker.py`：本机接收服务和连接生命周期。正式桌面 WebView 通过受密钥保护的 loopback `/desktop/*` 接口控制后台服务，不再维护第二套浏览器控制台。
- `worker.py`、`warm_worker.py`、`jobrunner.py`、`appserver_client.py`：Codex 执行进程、活跃期与任务状态。
- `panel.py`、`team_service.py`：协作 HTTP 服务和 Tailscale Serve 管理。
- `event_pump.py`：Desktop 独立事件等待线程；不经过同步 WebView 动作锁。
- `health.py`、`diagnostics.py`：跨平台稳定健康 schema 与拒绝默认导出的安全诊断快照。
- `mcp_adapter.py`、`mcp_server.py`：Codex 中使用协作功能的 stdio MCP 接口。
- `common.py`：各进程共享的文件、锁、校验和代理辅助函数。

Windows 和 macOS 的后台运行目录保存为 `client/agent_harness/`，而不是一组隐式顶层模块。运行时只把 `client/` 加入解释器搜索路径；可变账户数据、项目工作目录和任务目录永远不会成为 Python 导入路径。旧版内容寻址运行时可继续完成正在执行的任务，新版本启动的新服务使用新的包目录。

跨进程入口分为 `desktop_api.py`、`node_api.py` 和签名账户 `hub_api.py`。它们引用
`api_contracts.py` 中版本化、可枚举且不可变的动作契约，在进入有状态业务 handler 前拒绝未知动作、
非对象请求和缺失的直接必填字段。Desktop 会话/生命周期处理以及 Node 后台执行路由已经迁入所属边界
模块；复杂负载仍由所属业务层完成语义和权限校验。

数据库 schema v4 为每个账户维护独立事件序列，移除了旧 Web 用户名、密码哈希和 Cookie 会话表，并保存独立文件传输状态、短期凭据摘要及不含本机路径的项目工作区就绪状态。
事件只保存项目、类别和时间，不保存消息、任务合同、收发者或附件；群聊和 Agent 内部对话唤醒项目
成员，人类私聊只推进参与者自己的序列。Desktop WebView 由原生事件泵通过签名账户 RPC 等待游标，
收到事件后重新读取既有授权快照，因此事件通道本身不成为第二套数据读取接口。

速传文件字节不进入 SQLite：协作服务只在数据根目录的 `file-transfers/` 临时中转，控制面使用签名账户 RPC 请求、确认、取消和签发短期凭据，数据面使用限定分块大小的原始 HTTP Range/Content-Range。服务端逐块校验并在完成时再次流式核对整文件 SHA-256；接收设备确认保存后立即删除中转字节。没有固定总大小上限，但服务与目标机都会预检可用磁盘空间。

团队地址的 `/` 只显示迁移说明，`/healthz` 只公开协议和存活状态。人类协作入口是 Desktop 或本机
Codex MCP；旧 `/api/session`、`/api/action`、Cookie 页面和浏览器 SSE 均返回 `410 Gone`。

正式运行链路使用 Pull Broker：本机接收端领取队列任务并在本机启动 Codex。源码中不再包含旧的 SSH 远程执行、反向代理或动态部署控制器；机器之间的连接只负责协作服务和任务队列通信，执行权限仍由机器主人本地配置决定。

1.0 是干净的数据格式基线。正式代码直接创建当前数据库结构，不包含试验版数据库、版本化目录或旧 MCP 授权的自动迁移逻辑。

运行状态不写入源码树。正式应用统一使用 `%LOCALAPPDATA%\AgentHarness\data`，构建输出和缓存由 `.gitignore` 排除。
