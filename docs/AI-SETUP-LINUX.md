# 让服务器 Codex 配置 Linux 接收端

Agent Harness 的 Linux 发布包根目录包含 `交给AI安装.md`。它是给服务器端 Codex 阅读的受限运维任务说明，不要求用户理解 systemd、CPU 架构、Codex CLI 路径或 Tailscale userspace 模式。

推荐流程：

1. 在 Windows/macOS Harness 中打开 **账户 → 添加 Linux 服务器**。
2. 选择服务器需要接入的 HTTPS 协作服务。
3. 点击 **复制给服务器 Codex**。
4. 通过 SSH 或 VS Code Remote SSH 打开服务器上的 Codex，粘贴提示词。
5. Codex 自动预检并配置；只在 Tailscale 授权、Harness 设备批准、sudo、解锁密码或 linger 等权限节点暂停。
6. 服务器显示设备关联码后，在 Harness **账户 → 输入新电脑的关联码** 中核对批准。
7. 配置完成后，在控制台“我的 Agents”中选择该 Linux 机器添加员工。

如果还没有桌面端可生成带地址的提示词，也可以让服务器 Codex 阅读发布包中的 `交给AI安装.md`；它会先询问团队服务地址，不会猜测或把 `100.x` Tailscale 机器地址当作服务地址。

提示词不会包含密码、私钥、Codex 凭据、Tailscale auth key 或设备令牌。协作服务地址不是认证凭据，但只会在用户主动点击生成时写入剪贴板。

完整的 Linux 人工命令与故障排查仍保留在 [LINUX.md](LINUX.md)，用于没有 AI 终端或需要人工审计的环境。
