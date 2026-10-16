# 源码结构

Agent Harness 使用 `src` 布局，把可运行源码与界面资源、测试和维护脚本分开。发布脚本会把运行时需要的文件复制成便携包；用户无需理解源码目录，也不需要安装 Python。

## 顶层目录

| 目录 | 职责 |
|---|---|
| `src/agent_harness/` | 桌面应用、账户身份、项目协作、执行端和 MCP 源码 |
| `assets/web/` | 桌面 WebView 和团队服务使用的 HTML、CSS、JavaScript |
| `scripts/` | Windows 便携包构建脚本 |
| `tests/` | 单元、集成、界面行为和干净安装验证 |
| `docs/` | 新手教程与维护文档 |
| `.github/workflows/` | 按版本标签构建 GitHub Release |

## Python 模块职责

`src/agent_harness/` 内的模块按运行边界命名：

- `production_app.py`、`app_bridge.py`：桌面窗口与受限本机 API。
- `account_*`、`owner_*`：账户身份、设备归属与机器主人的授权边界。
- `collaboration_store.py`、`broker.py`、`coordinator.py`、`project_memory.py`：项目数据、消息、任务路由与项目记忆。
- `desktop_*`、`pull_worker.py`、`local_panel.py`：本机接收服务和连接生命周期。
- `worker.py`、`warm_worker.py`、`jobrunner.py`、`appserver_client.py`：Codex 执行进程、活跃期与任务状态。
- `panel.py`、`team_service.py`：协作 HTTP 服务和 Tailscale Serve 管理。
- `mcp_adapter.py`、`mcp_server.py`：Codex 中使用协作功能的 stdio MCP 接口。
- `common.py`：各进程共享的文件、锁、校验和代理辅助函数。

运行状态不写入源码树。正式应用统一使用 `%LOCALAPPDATA%\AgentHarness\data`，构建输出和缓存由 `.gitignore` 排除。
