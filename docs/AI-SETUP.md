# 让另一台机器上的 AI 完成安装

Harness 为三种目标机器各准备了一份提示词，交给**那台机器上的 AI**（Codex 或 DeepSeek Harness）阅读并执行。提示词本身就是一份受限运维任务说明：它先做只读预检，再下载官方发布包、校验 SHA-256、安装，最后把登录交还给人。

| 目标机器 | 提示词文件 | 它负责什么 |
|---|---|---|
| Linux 服务器 | `linux-ai-setup.md`（发布包里叫 `交给AI安装.md`） | 解压发布包、关联账户、安装 systemd 用户服务，作为无界面接收端 |
| Windows 电脑 | `windows-ai-setup.md` | 下载便携 zip、校验 SHA-256、解压到 `%LOCALAPPDATA%\Programs` 并启动 |
| macOS 电脑 | `macos-ai-setup.md` | 下载 arm64 包、校验、放进 `~/Applications` 并启动 |

推荐流程：

1. 在已登录的 Harness 中打开 **账户 → 在另一台电脑上安装**。
2. 选择目标机器类型，以及它要接入的 HTTPS 协作服务。
3. 点击 **复制给那台机器的 AI**。
4. 在那台机器上打开 AI（Linux 通常是 SSH 或 VS Code Remote SSH 里的 Codex），粘贴提示词。
5. AI 自行预检、下载、校验并安装；只在管理员权限、Tailscale 授权、设备批准、解锁密码、Gatekeeper 放行等节点停下等你决定。
6. 需要批准设备时，AI 会把关联码给你，在 **账户 → 输入新电脑的关联码** 中核对批准。
7. 登录、新建账户或恢复，由你本人在那台机器的窗口里完成——提示词明确禁止 AI 接触密码和恢复包。

提示词不包含密码、私钥、Codex 凭据、Tailscale auth key 或设备令牌。协作服务地址不是认证凭据，但只会在用户主动点击生成时写入剪贴板。

三份提示词也随应用一起分发（`assets/web/*-ai-setup.md`），Linux 发布包根目录另外带一份 `交给AI安装.md`。没有 AI 终端、或需要人工审计时，人工命令见 [LINUX.md](LINUX.md) 与 [MACOS.md](MACOS.md)。
