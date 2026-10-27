# 1.1.1 Linux 安全与运维修复

- 默认拒绝以 root 运行 Linux Harness；帮助命令仍可查看。
- 增加 Codex CLI 路径设置、自动检测和状态命令，适配 VS Code Remote 扩展升级。
- 连续升级使用真实当前 runtime 作为回滚目标；失败 release 可用 `--retry` 安全重试。
- Linux tar 限制文件数量、单文件和总解压体积，并拒绝重复路径。
- broker proxy 在长连接 MCP 的下一次请求中热加载；Codex 模型代理会安全退役常驻 worker，活跃任务不会被中断。
- `service logs` 同时显示 journal 与有界 receiver 日志；`doctor` 增加严格目录权限、磁盘、项目目录和 GPU 状态。
- 控制台明确说明“声明修改范围”不是文件系统 ACL，避免把提示合同误认为硬隔离。
