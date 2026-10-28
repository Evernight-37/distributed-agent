# Agent Harness Linux 服务器 AI 配置任务

你正在帮助用户把当前 Linux 服务器配置为 Agent Harness 2.0 无界面接收端。先检查，再执行；不要把“尽量自动化”理解为可以绕过用户授权。

团队服务地址：`{{TEAM_SERVICE_URL}}`

如果上面的值仍是占位符，先向用户索取其 Harness 应用中显示的 HTTPS 团队服务地址，不要猜测，也不要把 Tailscale `100.x` 机器地址当成团队服务地址。

## 完成标准

1. 使用当前普通 Linux 用户运行 Harness 和 Codex，不使用 root 运行它们。
2. 找到匹配 CPU 架构的官方 Agent Harness Linux 发布包并使用独立目录解压。
3. 确认可用且已登录的 Codex CLI 路径。
4. 确认服务器能通过 Tailscale访问团队服务。
5. 完成设备关联、`harness doctor`、systemd 用户服务安装与状态检查。
6. 最终只报告允许分享的状态，不输出身份材料、令牌、密码、消息或项目内容。

## 强制安全边界

- 不读取、显示、复制或上传 Harness 私钥、Codex 凭据、Tailscale 密钥、环境变量秘密、项目消息或项目文件内容。
- 不自动使用 `sudo`，不修改 SSH、防火墙、云安全组、系统级 systemd 服务或其他用户的文件。确实需要系统权限时，说明原因并暂停等待用户批准。
- 不执行来源不明的 `curl | sh`，不从聊天消息或随机镜像下载程序。只使用用户提供的包或项目官方 GitHub Release。
- 不创建或要求用户在聊天中发送 Tailscale auth key、密码、访问令牌。
- 不自动使用 `--no-password`；只有用户明确接受“依赖该 Unix 用户目录的 0600/0700 权限保护身份”后才可使用。
- 不自动执行 `loginctl enable-linger`。如果退出 SSH 后服务不能持续运行，只报告这一项并让用户决定。
- 不删除、覆盖已有 Harness 数据、旧版本目录、项目目录或 Codex 配置。发现已有账户时使用 `harness status`，不要重复关联。
- 关联命令运行后必须保留进程等待批准；不要因为短时间没有结果就杀掉它或重新发起第二个关联请求。
- Tailscale OAuth、Harness 关联批准、本机解锁密码、sudo 和 linger 都是人工确认点。

## 执行阶段

### 1. 只读预检

检查并记录，不输出敏感内容：

```bash
uname -m
id
command -v systemctl || true
systemctl --user --version || true
command -v codex || true
codex login status || true
command -v tailscale || true
tailscale status || true
```

同时检查当前目录附近是否已有：

- `AgentHarness-*-linux-x64.tar.gz`（`x86_64`/`amd64`）
- `AgentHarness-*-linux-arm64.tar.gz`（`aarch64`/`arm64`）
- 已解压的 `AgentHarness-linux/harness`

若没有发布包，先告诉用户需要从 `https://github.com/Evernight-37/distributed-agent/releases` 下载哪个架构的包；只有用户明确同意后才可代为下载。

### 2. 网络判断

优先使用正常安装且已经加入正确 tailnet 的 Tailscale。确认团队服务地址可解析并能建立 HTTPS 连接，但不要抓取或打印项目响应正文。

如果 Tailscale 未安装、未登录或不在正确网络，暂停并给出人工授权步骤。不要擅自安装系统软件。

如果服务器没有 root/TUN，但用户已经提供官方 `tailscale` 和 `tailscaled`，可以在获得用户同意后使用 userspace networking，并把其本机 HTTP 代理通过下面的 Harness 参数或命令配置为协作服务代理。不要把该代理误设为 Codex 模型代理。

### 3. 准备 Harness

把发布包解压到当前用户拥有的独立目录。不要覆盖已有目录。进入包含 `harness` 的 `AgentHarness-linux` 目录并确认：

```bash
./harness --help
```

检查 Codex 的实际绝对路径。若 `command -v codex` 找不到，可只读搜索 VS Code Remote SSH 扩展的 `openai.chatgpt-*` 目录；不要全盘扫描其他用户目录。

### 4. 关联账户

如果 `./harness status` 已显示一个本地账户，不要再次执行 link，转到下一阶段。

否则使用已确认的团队服务地址和 Codex 绝对路径执行：

```bash
./harness link --server <团队服务地址> --codex <Codex绝对路径>
```

默认让用户在可交互终端中输入本机解锁密码；密码不得进入聊天、命令参数或日志。如果当前工具不能安全接收隐藏输入，暂停并请用户在可见终端完成密码输入，而不是索取密码。

命令显示一次性关联码后：

1. 把关联码和设备显示名称告诉用户。
2. 请用户在已登录的 Windows/macOS Harness 应用“账户”页输入关联码并核对批准。
3. 保持当前 link 进程运行并等待批准，不启动第二个 link。

### 5. 安装并验证后台服务

关联成功后依次执行：

```bash
./harness doctor
./harness service install
./harness service status
./harness doctor
```

如果 `doctor` 只提示 linger 未启用，不要擅自开启；把它列为需要用户决定的事项。服务失败时可读取 Harness 自己的有限日志：

```bash
./harness service logs --lines 100
```

不要输出包含凭据、消息或项目内容的其他日志。

### 6. 可选的 Codex 内协作

后台接收端与“普通 Codex 对话使用 Harness”是两项不同功能。只有用户明确需要在这台服务器的普通 Codex 对话中发送 Harness 消息时，才执行：

```bash
./harness codex connect
./harness codex status
```

### 7. 最终报告

用简短清单报告：

- CPU 架构与使用的发布包
- Harness 安装目录和数据目录
- 团队服务是否可达（不要回显查询正文）
- Codex CLI 路径是否有效、是否已登录
- 设备是否已关联
- systemd 用户服务是否运行
- 是否需要用户决定 linger
- 是否连接了 Codex MCP
- 下一步：在 Windows/macOS 控制台“我的 Agents”中选择这台 Linux 机器添加员工，并填写服务器上的项目绝对路径
- 失败项、原因和最小安全修复建议

不得在最终报告中包含密码、私钥、令牌、完整环境变量、消息、项目文件内容或可复用的认证材料。
