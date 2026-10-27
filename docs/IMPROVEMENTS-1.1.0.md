# 1.1.0 Linux 节点产品化

- Linux 接收端可安装为 `systemd --user` 服务，支持启动、停止、重启、状态、日志和卸载；卸载不会删除账户或项目数据。
- Remote SSH 中的 Codex CLI 可通过受限 stdio MCP 连接 Harness，读取有权项目、发送消息和给自己的 Agent 派发异步任务。
- 协作服务代理与 Codex 模型代理分别配置，并提供不发起模型调用的健康检查。
- `doctor` 按接收端报告服务、Codex 登录、代理和本地权限状态，不输出令牌、私钥或消息。
- Linux 升级采用显式本地包和 SHA-256 校验、拒绝危险 tar 条目、并排安装、原子切换与失败回滚。
- 新增 Linux systemd、接收端、代理、MCP 和升级安全测试。
