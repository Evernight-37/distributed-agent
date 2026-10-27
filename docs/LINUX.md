# Linux 无界面接收端（1.1.1）

Linux 接收端把服务器作为你账户名下的一台执行机器。Windows 或 macOS 控制台可以在这台机器上添加员工、选择服务器项目目录并派入项目；群聊呼叫员工后，它会启动独立 Codex 会话并回传结果。接收端不会接管 VS Code 中已经打开的 Codex 对话。

需要 Python 3.10+、Python 的 `venv` 组件、可运行且已登录的 Codex CLI，以及到协作服务的网络连接。服务器项目文件始终留在服务器。普通用户即可安装，不修改系统 Python，也不需要 Linux 桌面环境。Ubuntu/Debian 的精简安装若缺少 `venv`，先由管理员一次性安装对应包（例如 Ubuntu 24.04 的 `python3-venv`）；此后安装和运行 Harness 都使用普通用户。不要使用 `sudo` 或 root 运行 Harness、Codex 或 MCP；1.1.1 会拒绝实际 root 操作。

## 首次安装

```bash
tar -xzf AgentHarness-1.1.1-linux-receiver.tar.gz
cd AgentHarness-linux
python3 install.py
./harness link --server https://你的团队地址 --codex /绝对路径/codex
```

在任一已登录的 Harness 控制台账户页输入关联码，核对设备名称后批准。可使用 VS Code 扩展自带的 Codex CLI，例如 `~/.vscode-server/extensions/openai.chatgpt-*/bin/linux-x86_64/codex`；必须把通配符替换成实际路径。扩展更新后若旧路径被删除，需要重新关联或更新 CLI 路径。

默认要求设置本机解锁密码。无人值守服务器可显式使用 `--no-password`，依靠用户私有目录和 `0600/0700` 权限保护设备身份。密码不会进入命令行、日志或协作服务。

关联后先检查：

```bash
./harness doctor
```

## 后台常驻

推荐安装为当前 Linux 用户的 systemd 服务：

```bash
./harness service install
./harness service status
./harness service logs --lines 100
```

其他命令：

```bash
./harness service start
./harness service stop
./harness service restart
./harness service logs --follow
./harness service uninstall
```

卸载服务只删除 `~/.config/systemd/user/agent-harness-receiver.service`，不会删除账户、项目绑定或服务器文件。若 `doctor` 显示 linger 未启用，退出所有 SSH 会话后用户服务可能停止；接收端只提示该状态，不会自行提权修改系统设置。

也可临时前台运行 `./harness run`，但不建议把 tmux 作为长期守护方案。

数据默认位于 `~/.local/share/AgentHarness/data`。

## 在 Remote SSH 的 Codex 中协作

关联完成后执行：

```bash
./harness codex connect
./harness codex status
./harness codex path detect
./harness codex path set /新的/绝对路径/codex
./harness codex path status
```

连接后，Linux 当前用户的普通 Codex 对话可以列出 Harness 项目、读取有权会话、发送消息，并给自己名下的 Agent 派发异步任务。它不能邀请成员、修改管理员、控制别人机器或批准执行命令。

断开：

```bash
./harness codex disconnect
```

MCP 使用数据目录中的稳定启动器。并排升级会更新启动器内容而保持 Codex 注册名称和命令路径不变。

## 网络与代理

协作服务代理和 Codex 模型代理彼此独立：

```bash
./harness network broker-proxy set http://127.0.0.1:18766
./harness network broker-proxy clear

./harness network codex-proxy set --port 7897
./harness network codex-proxy clear

./harness network status
./harness network test
```

两类代理都只接受本机回环地址。修改协作服务代理后执行 `./harness service restart`；长连接 MCP 会在下一次请求时重新读取代理。模型代理保存后会让空闲常驻 worker 退出，活跃 worker 完成本轮后退出，因此新代理从下一次新 worker 生效。`network test` 会检查端口、接收端和 Codex 登录状态，但不会发起模型调用。

服务器无法安装内核 Tailscale 时，可在用户目录启动官方 userspace 模式：

```bash
tailscaled --tun=userspace-networking \
  --socket="$HOME/.local/share/AgentHarness/tailscale/tailscaled.sock" \
  --statedir="$HOME/.local/share/AgentHarness/tailscale/state" \
  --outbound-http-proxy-listen=127.0.0.1:18766
tailscale --socket="$HOME/.local/share/AgentHarness/tailscale/tailscaled.sock" up \
  --hostname=linux-agent --accept-dns=false
./harness network broker-proxy set http://127.0.0.1:18766
./harness service restart
```

先在浏览器完成 Tailscale 授权，并选择与团队服务相同的网络。Tailscale 和 Harness 服务都必须保持运行。

## 安全升级与回滚

升级不会覆盖当前目录，也不会覆盖账户身份、项目绑定或服务器项目数据；它只会更新数据目录中的稳定 MCP 启动器。下载新 tar 包及其可信来源提供的 SHA-256 后执行：

```bash
./harness upgrade \
  --bundle /绝对路径/AgentHarness-1.1.1-linux-receiver.tar.gz \
  --sha256 64位十六进制摘要
```

接收端会拒绝路径穿越、链接和设备文件，检查摘要，在数据目录旁并排安装新版本；有未结束任务时拒绝切换。新服务启动失败会自动恢复旧服务。需要人工回退时执行：

```bash
./harness rollback
```

若包已经完整安装、但上次服务切换失败，可验证同一个包和摘要后重试：

```bash
./harness upgrade --bundle /绝对路径/同一个包.tar.gz --sha256 原摘要 --retry
```

不要从聊天消息或不可信网页复制摘要；摘要必须来自你信任的发布渠道。旧版本目录会保留用于回滚，不会自动删除。

## 已知限制

- Linux 端没有图形界面，控制台仍在 Windows 或 macOS；日常人工开发建议继续使用 VS Code Remote SSH。
- 附件可在成员间传输和预览，但尚不会自动作为图片或文件输入交给 Codex。
- 接收端不调度 GPU/显存资源；多个 Agent 并行时仍需机器所有者合理配置并发。
- `systemd --user` 是否能在无人登录时持续运行取决于服务器的 linger 策略。
- 实现任务的“声明修改范围”是提示合同，不是文件 ACL；实现阶段可写机器所有者登记的整个项目工作区。严格隔离应使用专用 Git worktree 和专用普通 Unix 用户。
