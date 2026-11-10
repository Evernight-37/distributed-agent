# Agent Harness Windows 电脑 AI 安装任务

你正在帮助用户在这台 Windows 电脑上安装 Agent Harness 桌面应用。安装并启动之后，**登录、新建账户或恢复、设备批准都由用户本人在窗口里完成**；你不接触账户密码、恢复包、私钥或任何令牌。

团队服务地址：`{{TEAM_SERVICE_URL}}`

如果上面的值仍是占位符，先向用户索取他在 Harness 应用中显示的 HTTPS 团队服务地址；不要猜测，也不要把 Tailscale 的 `100.x` 机器地址当成团队服务地址。

## 完成标准

1. 使用当前登录的普通用户安装到用户目录，不需要管理员权限，不安装系统级服务。
2. 从官方 GitHub Release 下载匹配版本的 Windows 包，并用随包提供的 SHA-256 校验。
3. 应用能启动并出现窗口。
4. 把登录与设备批准交给用户完成。
5. 最终只报告允许分享的信息：版本、安装路径、前提条件是否满足、是否已启动。

## 强制安全边界

- 不读取、显示、复制或上传 Harness 凭据、恢复包、密码、令牌、环境变量秘密。
- 不自动请求管理员权限，不修改系统级 PATH、注册表策略、防火墙或其他用户目录。
- 不执行来源不明的脚本，不从聊天消息或随机镜像下载程序，只用下面给出的官方 Release 资产。
- 不静默安装：默认使用便携 zip 解压到用户目录。只有用户明确要求时才可使用 `-setup.exe`，并让安装向导保持可见。
- 不添加防火墙规则。发送文件时 Windows 会弹一次授权，由用户自己点“允许”。
- 发现已经安装过 Harness（安装目录或 `%LOCALAPPDATA%\AgentHarness\data` 已存在）时不要覆盖或删除，报告现状后停止。
- 不代替用户登录，不索取密码或恢复包，不操作窗口内的任何输入框。

## 执行阶段

### 1. 只读预检

```powershell
$PSVersionTable.PSVersion.ToString()
[Environment]::Is64BitOperatingSystem
Test-Path "$env:LOCALAPPDATA\Programs\AgentHarness"
Test-Path "$env:LOCALAPPDATA\AgentHarness\data"
Test-Path "${env:ProgramFiles(x86)}\Microsoft\EdgeWebView\Application"
```

记录：是否 64 位系统、是否已有安装或数据、是否有 WebView2 运行时。**WebView2 缺失就停下来报告**——安装它需要管理员权限，由用户决定，不要代劳。

### 2. 下载并校验

从 `https://github.com/Evernight-37/distributed-agent/releases/latest` 取 `AgentHarness-<版本>-windows-x64.zip`，以及同名的 `.sha256`。只有用户明确同意后才可代为下载。

```powershell
$zip = Join-Path $env:TEMP "AgentHarness-<版本>-windows-x64.zip"
Invoke-WebRequest -Uri "<zip 下载地址>" -OutFile $zip
Invoke-WebRequest -Uri "<zip 下载地址>.sha256" -OutFile "$zip.sha256"
$expected = ((Get-Content "$zip.sha256" -Raw) -split '\s+')[0].ToLower()
$actual = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
$actual -eq $expected
```

结果必须是 `True`。不相等就停止，不要执行包里的任何文件。

### 3. 解压到用户目录（便携安装）

zip 的顶层就是 `AgentHarness\` 目录。

```powershell
$target = Join-Path $env:LOCALAPPDATA "Programs\AgentHarness"
if (Test-Path $target) { "已存在，报告后停止" } else {
  Expand-Archive -Path $zip -DestinationPath (Join-Path $env:LOCALAPPDATA "Programs")
}
Test-Path (Join-Path $target "AgentHarness.exe")
Get-Content (Join-Path $target "VERSION")
```

### 4. 启动并交给用户

```powershell
Start-Process (Join-Path $target "AgentHarness.exe")
Start-Sleep -Seconds 10
Get-Process AgentHarness | Select-Object Id, MainWindowTitle
```

进程要在，且 `MainWindowTitle` 里能看到 Harness 的窗口标题。如果窗口里出现 `Unhandled exception`、或日志里出现 `pythonnet`/`clr` 一类的加载错误，说明这个目录不适合运行（网络盘、受管理的同步目录等）：把整个 `AgentHarness` 目录挪到 `%LOCALAPPDATA%\Programs\AgentHarness` 再试，不要改系统设置或装 .NET 组件。

窗口出现后告诉用户：

- 数据保存在 `%LOCALAPPDATA%\AgentHarness\data`，程序在 `%LOCALAPPDATA%\Programs\AgentHarness`，整个目录可以直接备份或复制；
- 接下来由他本人完成登录：新建账户，或用恢复包/已有账户登录；如果账户里已经绑定过其他设备，需要在已登录的机器上批准这台新设备；
- 团队服务地址是上面那个 HTTPS 地址，登录后在应用里选择它。

### 5. 最终报告

- Windows 版本与是否 64 位
- 使用的发布包与版本
- SHA-256 校验是否通过
- 安装路径与数据目录是否已存在过
- WebView2 是否满足
- 应用是否已启动
- 需要用户完成的事项（登录、设备批准）
- 失败项、原因和最小安全修复建议

不得在报告中包含密码、恢复包内容、私钥、令牌或环境变量原文。
