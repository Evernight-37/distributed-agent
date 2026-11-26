"use strict";

// Bridge errors are also used by older servers and the diagnostic log.
// Keep their source text intact and translate known messages at the UI boundary.
const desktopMessages = new Map([
  ["请先登录", "Sign in first."],
  ["请先解锁账户", "Unlock your account first."],
  ["请先退出当前账户", "Sign out of the current account first."],
  ["本地账户不存在", "This local account does not exist."],
  ["密码错误", "Incorrect password."],
  ["账户名不能为空", "Enter an account name."],
  ["关联请求不存在", "The device link request was not found."],
  ["本机没有待关联请求", "There is no pending link request for this computer."],
  ["恢复包无效或过大", "The recovery file is invalid or too large."],
  ["恢复包不是有效的 JSON 文件", "The recovery file is not valid JSON."],
  [
    "恢复包只能在桌面应用中导出",
    "Open the desktop app to export a recovery file.",
  ],
  [
    "恢复包只能在桌面应用中导入",
    "Open the desktop app to import a recovery file.",
  ],
  ["协作服务未连接", "The collaboration service is disconnected."],
  ["未知服务", "Unknown collaboration service."],
  ["请先加入该协作服务", "Join this collaboration service first."],
  ["请先加入该服务上的项目", "Join a project on this service first."],
  [
    "请先接受这个服务的项目邀请",
    "Accept a project invite for this service first.",
  ],
  [
    "只能在自己账户名下的协作服务创建项目",
    "You can only create projects on a collaboration service you own.",
  ],
  [
    "只能在协作服务所在的电脑管理备份",
    "Manage backups on the computer hosting the service.",
  ],
  [
    "只能配置或移除你在此项目里的 Agent",
    "You can only configure or remove your agents in this project.",
  ],
  [
    "请先把本机接收端接入该项目的协作服务",
    "Connect this computer’s receiver to the project’s collaboration service first.",
  ],
  [
    "请先在设置中将本机 agent 接入该协作服务",
    "Connect this computer’s receiver to the collaboration service in Settings first.",
  ],
  [
    "请先确认本机原任务的进程已停止",
    "Confirm the original task process has stopped on this computer.",
  ],
  ["项目工作目录配置失败", "Could not configure the project working folder."],
  [
    "找不到等待批准的本机执行任务",
    "No local execution awaiting approval was found.",
  ],
  ["执行任务编号无效", "Invalid execution ID."],
  ["本机还没有接收目录", "Set a receive folder for this computer first."],
  [
    "请填写目标机器上的绝对接收目录",
    "Enter an absolute receive folder path on the destination computer.",
  ],
  [
    "请选择当前账户已接入的协作服务",
    "Choose a collaboration service connected to your account.",
  ],
  ["文件夹选择器尚未就绪", "The folder picker is not ready."],
  ["文件选择器尚未就绪", "The file picker is not ready."],
  ["文件保存窗口尚未就绪", "The save dialog is not ready."],
  ["代理端口必须在 1 到 65535 之间", "Enter a proxy port from 1 to 65535."],
  ["请指定模型", "Choose a model."],
  ["模型名不能为空", "Enter a model name."],
  ["模型表至少要有一个模型", "Add at least one model."],
  [
    "模型名只能包含字母、数字、下划线、点、冒号和连字符",
    "Model names may contain letters, numbers, underscores, periods, colons and hyphens.",
  ],
  [
    "暂时连不上发布页，请稍后再试",
    "The release page is unavailable. Try again later.",
  ],
  [
    "更新检查已关闭，请先开启检查频率",
    "Update checks are off. Choose a check frequency first.",
  ],
  ["已经是最新版本，没有可下载的更新", "You already have the latest version."],
  ["还没有检查到可下载的版本", "No downloadable update has been found yet."],
  ["没有已下载并校验的更新", "No verified update is ready to install."],
  ["安装包不存在，请重新下载", "The package is missing. Download it again."],
  [
    "发布页没有提供这个安装包的校验值，已停止下载",
    "This release has no checksum for the package. Download stopped.",
  ],
  [
    "安装包校验失败，已删除下载内容",
    "Package verification failed. The download was removed.",
  ],
  [
    "安装包大小与发布信息不符，已删除下载内容",
    "The package size did not match the release. The download was removed.",
  ],
  ["没有可以回退的上一个版本", "There is no previous version to restore."],
  ["当前已经运行在这个版本上", "You are already running this version."],
  [
    "托管端尚不支持附件。请将协作服务升级到当前版本并重启后再试。",
    "The host does not support attachments yet. Update and restart the collaboration service, then try again.",
  ],
  [
    "协作服务尚不支持文件速传。请将托管端升级到当前版本并重启后再试。",
    "The host does not support file transfers yet. Update and restart the collaboration service, then try again.",
  ],
  [
    "两台设备的系统时间相差过大，请校准两端时间后重试。",
    "The system clocks are too far apart. Synchronize the time on both devices and try again.",
  ],
  [
    "协作服务尚不支持项目任务。请升级托管端并重启协作服务。",
    "The host does not support project tasks yet. Update and restart the collaboration service.",
  ],
  [
    "协作服务版本过旧。请让托管电脑更新应用并重启协作服务后再发送消息。",
    "The service is out of date. Ask the host to update Foolship and restart the service before sending messages.",
  ],
  [
    "协作服务版本过旧，尚不能保护新会话的隐私。请让托管电脑更新应用并重启协作服务。",
    "The service is too old to protect new conversations. Ask the host to update Foolship and restart the service.",
  ],
  [
    "协作服务返回了无法解析的响应，它可能正在启动或重启；稍后再试即可。",
    "The service returned an unreadable response. It may be restarting. Try again shortly.",
  ],
]);

const desktopMessagePrefixes = [
  [
    "这个版本没有适用于本机的安装包：",
    "This release does not include a package for this computer: ",
  ],
  ["正在进行的任务会中断：", "Running tasks will be interrupted: "],
  [
    "账户在协作服务上被拒绝：",
    "The collaboration service rejected this account: ",
  ],
];

function localizeDesktopText(value) {
  if (typeof value !== "string" || interfaceLocale() !== "en") return value;
  if (desktopMessages.has(value)) return desktopMessages.get(value);
  for (const [prefix, english] of desktopMessagePrefixes) {
    if (value.startsWith(prefix)) return english + value.slice(prefix.length);
  }
  return value;
}
