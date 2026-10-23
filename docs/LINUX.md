# Linux 服务器接收端（1.0.2）

接收端没有图形界面。服务器作为自己的关联设备加入 Harness，由 Mac 控制台添加员工、选择服务器目录、派入项目；群里呼叫该员工后，它在服务器上启动独立 Codex 会话，结果回到群里。不会接管 VS Code 中已经打开的对话。

需要 Python 3.10+、可运行且已登录的 Codex CLI，以及到协作服务的网络连接。服务器项目文件留在服务器。普通用户即可安装，不需要修改系统 Python。

## 安装

```bash
tar -xzf AgentHarness-1.0.2-linux-receiver.tar.gz
cd AgentHarness-linux
python3 install.py
./harness link --server https://你的Mac团队地址 --codex /绝对路径/codex
```

在 Mac 的 Harness 账户页点击“输入新电脑的关联码”，核对并批准服务器。可用 VS Code 扩展自带的 Codex CLI，例如 `~/.vscode-server/extensions/openai.chatgpt-*/bin/linux-x86_64/codex`，需先把通配符替换为实际路径。更新扩展后如果旧路径被删除，需更新接收端的 `codex-cli.json`。

默认要求为服务器上的身份设置本机解锁密码。无人值守设备可显式传入 `--no-password`，依靠用户私有目录与文件权限保护凭据。后台接收使用单独的设备凭据，运行时不需要交互式输入密码。

启动：

```bash
./harness run
```

可放在独立的 tmux 会话中运行。查看 `./harness status`；停止用 `./harness stop`。数据位于 `~/.local/share/AgentHarness/data`，更新应用时保留该目录。

## 服务器未安装 Tailscale

可使用官方静态二进制在用户目录启动 userspace 模式：

```bash
tailscaled --tun=userspace-networking \
  --socket="$HOME/.local/share/AgentHarness/tailscale/tailscaled.sock" \
  --statedir="$HOME/.local/share/AgentHarness/tailscale/state" \
  --outbound-http-proxy-listen=127.0.0.1:18766
tailscale --socket="$HOME/.local/share/AgentHarness/tailscale/tailscaled.sock" up \
  --hostname=pku-fastgs-agent --accept-dns=false
```

先用浏览器完成 Tailscale 授权，并选择与 Mac 相同的团队网络，然后关联 Harness 时增加 `--broker-proxy http://127.0.0.1:18766`。这个代理只用于协作服务通信；Codex 的模型网络代理和已有 SSH 转发维持各自设置。Tailscale 与 Harness 接收端都必须保持运行。

## 在 Mac 中派遣服务器员工

1. 控制台添加员工，机器选择服务器。
2. 配置服务器项目的绝对目录，例如 `/mnt/hdd/gs_dev/FastGS`，选择服务器支持的模型、权限和任务阶段。
3. 把员工派入目标项目，等待设备确认并显示在线。
4. 群里从“呼叫 Agent”中选择该员工，发送任务。

1.0.2 的文件附件支持成员收发与图片预览；本版本尚不自动把附件作为 Codex 图像或文件输入。
