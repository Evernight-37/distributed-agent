# Agent Harness 1.0.1 改进版

Windows 和 macOS 使用相同的协作协议，两个分支都包含以下修复：

- 从“呼叫 Agent”选择其他成员的员工后，公开群消息可以正确唤醒目标员工。
- 选择“默认由管理员回复”时不再把选项文字作为员工 ID 发送。
- 控制台的员工卡片增加“删除”按钮。删除会停用该员工并退出所有项目；历史消息和本机工作文件保留。任务运行中会阻止删除，离线设备需上线确认后才能完成。
- 删除项目管理员员工时会清空管理员设置，群主可重新指定。
- 删除需要账户所有者授权，不能操作其他成员的员工。

## 升级

退出旧版应用后替换完整应用包。不要删除用户数据目录。首次打开新版可能需要重新解锁本机账户。

- Windows：替换整个 `AgentHarness` 文件夹，双击 `AgentHarness.exe`。数据保存在 `%LOCALAPPDATA%\AgentHarness\data`。
- macOS：将 `AgentHarness.app` 放入“应用程序”。数据保存在 `~/Library/Application Support/AgentHarness/data`。

群聊服务托管在另一台电脑时，托管方也需升级并完全重启应用及后台服务，才能修复群消息调度和提供删除员工协议。每台拥有员工的设备也需升级，才能确认删除命令。

## Windows 构建与下载

`windows-improvements` 分支推送后会在 GitHub Actions 的 **Build Windows improvements** 中生成完整便携版。打开成功的运行，在 Artifacts 下载 `AgentHarness-1.0.1-windows-x64`，解压外层下载包及其中的 zip，保留整个应用目录。

也可在 Windows 使用 Python 3.13 和 Node.js 24 手动构建：

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
npm ci
.venv\Scripts\python -m unittest discover -s tests -t . -q
npm test
.venv\Scripts\python scripts/build_desktop.py --output-root release
```

产物位于 `release/AgentHarness`。Windows 应用包含独立后台运行时，接收方不需安装 Python。
