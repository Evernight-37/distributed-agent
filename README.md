# Agent Harness

Agent Harness 是面向小型可信团队的分布式 Agent 协作应用。每个人管理自己电脑上的 Codex、模型、目录和执行权限；项目服务负责成员、会话、共享记忆与任务路由。

## 使用应用

本分支的 **Windows 1.0.2 改进版** 包含群聊文件和图片、员工删除、默认管理员发送及跨成员呼叫修复。完整便携包在 [Build Windows improvements](https://github.com/Evernight-37/distributed-agent/actions/workflows/windows-improvements.yml) 成功运行的 Artifacts 中下载；详见 [1.0.2 升级说明](docs/IMPROVEMENTS-1.0.2.md)。Linux 服务器可安装 [无界面接收端](docs/LINUX.md)，由 Mac 或 Windows 控制台管理。

从 [GitHub Releases](https://github.com/Evernight-37/distributed-agent/releases) 下载完整 Windows 压缩包，解压后双击 `AgentHarness.exe`。请保留整个目录；应用自带隔离的后台 Python 运行时，不要求用户安装 Python。系统需要 Edge WebView2，并且每台执行 Agent 的电脑需要已有可用的 Codex 登录。

第一次使用请先阅读 [按钮式新手教程](docs/QUICKSTART.md)。教程从 Tailscale 准备开始，分别列出群主、服务托管者和受邀同事应点击的按钮，并在每一步说明成功标志与常见错误。单机使用可以跳过 Tailscale。多人协作的主流程是：

1. 群主从 Tailscale 管理台邀请外部用户，双方确认设备在线。
2. 每个人分别创建自己的 Harness 账户；不要共享账户或设备关联码。
3. 群主在应用里创建团队入口，并分享 HTTPS 团队地址和项目邀请码。
4. 同事凭两项信息加入项目，再把自己的机器和 Agent 接入该服务。
5. 每个人配置和派遣自己的 Agent；群主选择管理员 Agent。

同一账户可以关联多台电脑。账户、接收端、MCP 授权和团队数据库位于 `%LOCALAPPDATA%\AgentHarness\data`；项目文件、Codex 登录、设备私钥和研究环境不会自动同步。

## 在 Codex 中协作

登录应用后，打开 **设置 → 在 Codex 中使用协作**，点击“连接到 Codex”。此后可以在普通 Codex 对话中列出 Harness 项目、读取有权查看的会话、发送消息，以及给自己名下的 Agent 派发异步任务。

该入口使用本机 stdio MCP，不开放额外端口。Codex 获得受限委托身份，不能邀请成员、修改管理员、控制别人的机器或批准执行命令。

## 多人和多机连接

项目数据保存在创建项目时选择的协作服务上。托管电脑可在 **设置 → 协作服务 → 创建团队入口** 中建立 Tailscale 私有 HTTPS 地址。同事加入同一 Tailscale 网络后，用该地址和项目邀请码加入项目；不需要为每个团队重新创建账户。Tailscale 显示的 `100.x.x.x` 是机器地址，不是团队服务地址；应用中可分享的地址形如 `https://电脑名.xxxx.ts.net`。

应用不会安装组网软件、开放防火墙或接管 Codex/GitHub 凭据。服务运营者能够读取其数据库中的项目内容，因此成员应信任托管者。

## 从源码验证和构建

运行测试：

```powershell
python -m pip install -r requirements-dev.txt
npm ci
ruff check src scripts tests
ruff format --check src scripts tests
python -m unittest discover -s tests -t . -v
npm run format:check
npm test
```

构建 Windows x64 便携包：

```powershell
python -m pip install -r requirements-build.txt
python scripts/build_desktop.py
```

构建结果位于 `dist/AgentHarness`。构建脚本下载并校验 Python.org 的嵌入式 Python 3.14.7，为后台接收端安装固定版本的 `cryptography`，然后生成完整便携目录。发布前还应运行：

```powershell
python tests/cleanroom_release.py dist/AgentHarness build/cleanroom
```

若要同时运行便携包接收端集成测试，请把 `AGENT_HARNESS_TEST_BUNDLE` 指向已构建的 `AgentHarness` 目录，再运行 `python -m unittest tests.test_desktop.PackagedReceiverTests -v`。

运行数据、令牌、密钥、日志、构建缓存和发布包均由 `.gitignore` 排除。Windows 可执行文件当前没有代码签名。

## 源码目录

```text
assets/web/          WebView 与浏览器界面
docs/                用户文档与架构说明
scripts/             构建、打包等维护脚本
src/agent_harness/   Python 应用与协作运行时源码
tests/               Python、JavaScript 与发布包测试
```

各目录的职责和主要模块见 [源码结构说明](docs/ARCHITECTURE.md)。根目录只保留项目说明、版本、依赖和仓库配置。
