# Agent Harness macOS 电脑 AI 安装任务

你正在帮助用户在这台 Mac 上安装 Agent Harness 桌面应用。安装并启动之后，**登录、新建账户或恢复、设备批准都由用户本人在窗口里完成**；你不接触账户密码、恢复包、私钥或任何令牌。

团队服务地址：`{{TEAM_SERVICE_URL}}`

如果上面的值仍是占位符，先向用户索取他在 Harness 应用中显示的 HTTPS 团队服务地址；不要猜测，也不要把 Tailscale 的 `100.x` 机器地址当成团队服务地址。

## 完成标准

1. 使用当前登录用户安装到 `~/Applications`，不需要 `sudo`，不安装系统级服务。
2. 确认这台 Mac 是 Apple Silicon（发布包只有 arm64）。
3. 从官方 GitHub Release 下载匹配版本的 macOS 包，并用随包提供的 SHA-256 校验。
4. 应用能启动并出现窗口；Gatekeeper 的放行由用户本人决定。
5. 把登录与设备批准交给用户完成，最终只报告允许分享的信息。

## 强制安全边界

- 不读取、显示、复制或上传 Harness 凭据、恢复包、密码、令牌、钥匙串内容。
- 不自动使用 `sudo`，不修改系统级目录、`/Applications` 之外的系统位置、防火墙或其他用户目录。
- 不执行来源不明的脚本，不从聊天消息或随机镜像下载程序，只用下面给出的官方 Release 资产。
- **不要自动移除隔离属性**（`xattr -d com.apple.quarantine`）。发布包未做 Apple 签名与公证，系统会拦一次；把命令和后果交给用户决定，或让用户用右键“打开”完成首次放行。
- 发现已经安装过 Harness（`~/Applications/AgentHarness.app` 或 `~/Library/Application Support/AgentHarness/data` 已存在）时不要覆盖或删除，报告现状后停止。
- 不代替用户登录，不索取密码或恢复包。

## 执行阶段

### 1. 只读预检

```bash
uname -m
sw_vers -productVersion
test -d ~/Applications/AgentHarness.app && echo installed || true
test -d ~/Library/Application\ Support/AgentHarness/data && echo has-data || true
```

`uname -m` 必须是 `arm64`。如果是 `x86_64`（Intel Mac 或 Rosetta 下的 shell），先确认是否为 Apple Silicon：`sysctl -n machdep.cpu.brand_string`；Intel 机器没有可用发布包，报告后停止。

### 2. 下载并校验

从 `https://github.com/Evernight-37/distributed-agent/releases/latest` 取 `AgentHarness-<版本>-macos-arm64.zip`，以及同名的 `.sha256`。只有用户明确同意后才可代为下载。

```bash
cd "$(mktemp -d)"
curl -fLO "<zip 下载地址>"
curl -fLO "<zip 下载地址>.sha256"
shasum -a 256 -c "AgentHarness-<版本>-macos-arm64.zip.sha256"
```

输出必须是 `OK`。失败就停止，不要解压执行。

### 3. 解包到用户目录

```bash
unzip -q AgentHarness-<版本>-macos-arm64.zip -d .
test -d AgentHarness.app
mkdir -p ~/Applications
ditto AgentHarness.app ~/Applications/AgentHarness.app
```

`ditto` 会保留签名相关的扩展属性；不要用 `cp -R` 替代。如果 `~/Applications/AgentHarness.app` 已存在，停止并报告。

### 4. 首次启动并交给用户

```bash
open ~/Applications/AgentHarness.app
```

如果系统提示“无法验证开发者”，说明这是未签名发布包，由用户决定：在“系统设置 → 隐私与安全性”里点“仍要打开”，或右键应用图标选择“打开”。你可以给出命令供用户自己执行，但**不要替他执行**：

```bash
xattr -d com.apple.quarantine ~/Applications/AgentHarness.app
```

窗口出现后告诉用户：

- 数据保存在 `~/Library/Application Support/AgentHarness/data`，程序在 `~/Applications/AgentHarness.app`；
- 接下来由他本人完成登录：新建账户，或用恢复包/已有账户登录；如果账户里已经绑定过其他设备，需要在已登录的机器上批准这台新设备；
- 团队服务地址是上面那个 HTTPS 地址，登录后在应用里选择它。

### 5. 最终报告

- macOS 版本与 CPU 架构
- 使用的发布包与版本
- SHA-256 校验是否通过
- 安装路径与是否已有数据目录
- Gatekeeper 是否已由用户放行
- 应用是否已启动
- 需要用户完成的事项（登录、设备批准）
- 失败项、原因和最小安全修复建议

不得在报告中包含密码、恢复包内容、私钥、令牌或钥匙串内容。
