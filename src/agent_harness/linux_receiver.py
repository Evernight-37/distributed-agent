"""Headless Linux account linking, receiver supervision, and local management."""

import argparse
import getpass
import json
import os
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from agent_harness.app_bridge import AppBridge
from agent_harness.broker_transport import broker_opener
from agent_harness.common import lock, read_json, validate_local_proxy, write_json
from agent_harness.desktop_runtime import DesktopRuntime
from agent_harness.health import HealthCode, component, report
from agent_harness.linux_service import ServiceManager
from agent_harness.worker import cli_status, codex_login_status, codex_version


def invoke(bridge, action, payload=None):
    value = bridge.invoke(action, payload)
    if not value.get("ok"):
        raise RuntimeError(value.get("error", action + " failed"))
    return value["result"]


def receiver_paths(root):
    receivers = Path(root) / "receivers"
    return sorted(
        path
        for path in receivers.glob("device-*")
        if path.resolve().parent == receivers.resolve() and (path / "pull-config.json").is_file()
    )


def _private_json(path, value):
    path = Path(path)
    write_json(path, value)
    path.chmod(0o600)


def _write_launcher(root, profile_id, python=None, adapter=None, standalone=None):
    """Refresh one stable MCP launcher without exposing its private grant."""
    root = Path(root).resolve()
    directory = root / "mcp"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    launcher = directory / f"launch-{profile_id}.sh"
    executable = str(Path(python or sys.executable).resolve())
    standalone = bool(getattr(sys, "frozen", False)) if standalone is None else bool(standalone)
    if standalone:
        command = [executable, "mcp-stdio"]
    else:
        command = [
            executable,
            "-X",
            "utf8",
            str(Path(adapter or (Path(__file__).resolve().parent / "mcp_adapter.py")).resolve()),
        ]
    command += [
        "--grant",
        str(directory / f"grant-{profile_id}.json"),
        "--network",
        str(root / "broker-network.json"),
    ]
    temporary = launcher.with_suffix(".tmp")
    temporary.write_text("#!/bin/sh\nexec " + shlex.join(command) + "\n", encoding="utf-8", newline="\n")
    temporary.chmod(0o700)
    os.replace(temporary, launcher)
    return launcher


def refresh_mcp_launchers(root, python=None, adapter=None, standalone=None):
    bridge = AppBridge(root)
    refreshed = []
    for profile in bridge._profiles():
        profile_id = profile.get("id") if isinstance(profile, dict) else None
        if isinstance(profile_id, str) and profile_id:
            refreshed.append(
                str(
                    _write_launcher(
                        bridge.root,
                        profile_id,
                        python=python,
                        adapter=adapter,
                        standalone=standalone,
                    )
                )
            )
    return refreshed


def unlock_bridge(root, password_reader=getpass.getpass):
    bridge = AppBridge(root)
    profiles = bridge._profiles()
    if len(profiles) != 1:
        raise ValueError("Linux 接收端需要且只能有一个本地账户；请先完成设备关联")
    profile_id = profiles[0].get("id")
    try:
        invoke(bridge, "login", {"profile": profile_id, "password": ""})
    except RuntimeError as exc:
        if "Unable to unlock local account" not in str(exc):
            raise
        password = password_reader("输入 Linux 接收端的本机解锁密码：")
        invoke(bridge, "login", {"profile": profile_id, "password": password})
    return bridge


def status(root):
    bridge = AppBridge(root)
    receivers = []
    for path in receiver_paths(root):
        item = DesktopRuntime(path).status()
        codex = cli_status(path)
        if codex.get("found"):
            codex.update(codex_login_status(codex["path"], path))
        item["codex"] = codex
        item["receiver"] = path.name
        receivers.append(item)
    return {
        "root": str(bridge.root),
        "profiles": invoke(bridge, "local-status")["profiles"],
        "receivers": receivers,
    }


def link(args):
    bridge = AppBridge(args.root)
    if bridge._profiles():
        raise ValueError("这台接收端已有账户，请使用 status 查看；不要重复关联")
    if args.broker_proxy:
        broker_opener(args.broker_proxy)
        _private_json(bridge.root / "broker-network.json", {"proxy": args.broker_proxy})
        bridge.broker_proxy = args.broker_proxy
    if args.codex:
        path = Path(args.codex).expanduser().resolve()
        if not path.is_file() or not codex_version(path):
            raise ValueError("请选择可运行的 Codex CLI")
        _private_json(bridge.root / "codex-cli.json", {"path": str(path), "mode": "manual"})
    password = "" if args.no_password else getpass.getpass("为服务器上的本机身份设置解锁密码：")
    request = invoke(
        bridge,
        "device-request",
        {"server": args.server, "name": args.name, "password": password},
    )
    print(
        "在任一已登录的 Harness 控制台账户页输入关联码，核对并批准这台 Linux 服务器：",
        flush=True,
    )
    print(request["code"], flush=True)
    print("设备名称：" + args.name, flush=True)
    deadline = time.monotonic() + min(request.get("expires_in", 600), 600)
    while time.monotonic() < deadline:
        result = invoke(bridge, "device-claim")
        if result.get("status") == "approved":
            refresh_mcp_launchers(bridge.root)
            print(
                "关联成功。现在可在 Windows 或 macOS 控制台为这台服务器添加员工并派入项目。",
                flush=True,
            )
            return
        time.sleep(2)
    raise RuntimeError("关联码已过期，请重新执行 link")


def run(root):
    paths = receiver_paths(root)
    if not paths:
        raise ValueError("请先执行 link 并在 Harness 控制台批准关联")
    stopped = threading.Event()
    stop_path = Path(root) / "linux-stop.json"
    stop_path.unlink(missing_ok=True)
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    with lock(Path(root) / "linux-receiver.lock"):
        try:
            while not stopped.is_set() and not stop_path.exists():
                for path in paths:
                    runtime = DesktopRuntime(path)
                    if not runtime.status()["running"]:
                        runtime.start()
                stopped.wait(5)
        finally:
            for path in paths:
                DesktopRuntime(path).stop_and_wait()


def codex_command(root, command):
    bridge = unlock_bridge(root)
    _write_launcher(bridge.root, bridge.profile["id"])
    if command == "connect":
        return bridge._enable_mcp()
    if command == "disconnect":
        return bridge._disable_mcp()
    return bridge._mcp_status()


def _sync_codex_path(root, selected):
    bridge = AppBridge(root)
    for receiver in receiver_paths(root):
        bridge._sync_cli(DesktopRuntime(receiver), selected)


def codex_path_command(root, action, selected=None):
    root = Path(root).resolve()
    saved_path = root / "codex-cli.json"
    if action == "set":
        candidate = Path(selected).expanduser()
        if not candidate.is_absolute():
            raise ValueError("Codex CLI 必须使用绝对路径")
        candidate = candidate.resolve()
        version = codex_version(candidate) if candidate.is_file() else None
        if version is None:
            raise ValueError("所选程序无法运行 Codex CLI")
        result = {
            "found": True,
            "configured": True,
            "path": str(candidate),
            "version": ".".join(map(str, version)),
            "mode": "manual",
        }
        _private_json(saved_path, result)
        _sync_codex_path(root, str(candidate))
        return result
    if action == "detect":
        result = cli_status(root)
        selected_path = result.get("path", "") if result.get("found") else ""
        stored = {
            "path": selected_path,
            "version": result.get("version", ""),
            "error": result.get("error", ""),
            "mode": "auto",
        }
        _private_json(saved_path, stored)
        _sync_codex_path(root, selected_path)
        return {**result, "mode": "auto"}
    saved = read_json(saved_path, {})
    path = saved.get("path") if isinstance(saved, dict) else None
    version = codex_version(path) if isinstance(path, str) and path and Path(path).is_file() else None
    if version is None:
        return {
            "found": False,
            "configured": bool(path),
            "path": path,
            "mode": saved.get("mode") if isinstance(saved, dict) else None,
            "error": "设置的 Codex CLI 路径无效；请运行 codex path set 或 codex path detect",
        }
    return {
        "found": True,
        "configured": True,
        "path": path,
        "version": ".".join(map(str, version)),
        "mode": saved.get("mode", "manual"),
    }


def _set_broker_proxy(root, proxy):
    root = Path(root)
    if proxy:
        broker_opener(proxy)
        _private_json(root / "broker-network.json", {"proxy": proxy})
    else:
        (root / "broker-network.json").unlink(missing_ok=True)
    for receiver in receiver_paths(root):
        path = receiver / "pull-config.json"
        with lock(receiver / "pull-config.lock"):
            config = read_json(path, {})
            if proxy:
                config["broker_proxy"] = proxy
            else:
                config.pop("broker_proxy", None)
            _private_json(path, config)
    return {
        "enabled": bool(proxy),
        "proxy": proxy,
        "receiver_restart_required": True,
        "codex_mcp_restart_required": False,
        "note": "请重启 Harness 服务；长连接 Codex MCP 会在下一次请求时自动重新读取代理",
    }


def _set_codex_proxy(root, setting):
    setting = validate_local_proxy(setting)
    root = Path(root)
    bridge = AppBridge(root)
    warnings = []
    for receiver in receiver_paths(root):
        runtime = DesktopRuntime(receiver)
        bridge._sync_proxy(runtime, setting)
        if runtime.status().get("running"):
            try:
                runtime._control("action", {"action": "retire-workers-after-turn"})
            except (OSError, RuntimeError, ValueError) as exc:
                warnings.append({"receiver": receiver.name, "error": str(exc)[:500]})
    _private_json(root / "codex-proxy.json", setting)
    result = {**setting, "applies_to": "next newly started Codex worker"}
    if warnings:
        result["warnings"] = warnings
        result["note"] = "代理已保存；部分常驻 worker 无法请求安全回收，请重启 Harness 服务"
    return result


def network_status(root):
    broker = read_json(Path(root) / "broker-network.json", {})
    codex = read_json(Path(root) / "codex-proxy.json", {"enabled": False, "port": None})
    return {
        "broker_proxy": {
            "enabled": bool(broker.get("proxy")),
            "address": broker.get("proxy"),
        },
        "codex_proxy": validate_local_proxy(codex),
    }


def _proxy_reachable(address):
    if not address:
        return None
    parsed = urlsplit(address)
    try:
        with socket.create_connection((parsed.hostname, parsed.port), timeout=2):
            return True
    except OSError:
        return False


def _disk_status(path):
    try:
        usage = shutil.disk_usage(path)
        return {"total_bytes": usage.total, "free_bytes": usage.free}
    except OSError as exc:
        return {"error": type(exc).__name__}


def _project_health(root):
    projects = []
    seen = set()
    for receiver in receiver_paths(root):
        for config_path in sorted((receiver / "projects").glob("*/project.json")):
            config = read_json(config_path, {})
            remote = config.get("remote_path") if isinstance(config, dict) else None
            if not isinstance(remote, str) or not remote:
                continue
            path = Path(remote).expanduser().resolve()
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            projects.append(
                {
                    "path": key,
                    "exists": path.is_dir(),
                    "readable": path.is_dir() and os.access(path, os.R_OK | os.X_OK),
                    "writable": path.is_dir() and os.access(path, os.W_OK | os.X_OK),
                    "disk": _disk_status(path) if path.is_dir() else None,
                }
            )
    return projects


def _gpu_status():
    executable = shutil.which("nvidia-smi")
    if not executable:
        return {"available": False}
    try:
        result = subprocess.run(
            [
                executable,
                "--query-gpu=name,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "error": type(exc).__name__}
    if result.returncode:
        return {
            "available": False,
            "error": (result.stderr or "nvidia-smi failed").strip()[:500],
        }
    devices = []
    for line in result.stdout.splitlines()[:16]:
        name, separator, memory = line.rpartition(",")
        if separator and memory.strip().isdigit():
            devices.append({"name": name.strip()[:200], "memory_total_mib": int(memory.strip())})
    return {"available": bool(devices), "devices": devices}


def doctor(root):
    root = Path(root).resolve()
    root_mode = stat.S_IMODE(root.stat().st_mode) if root.exists() else None
    network = network_status(root)
    codex_address = None
    if network["codex_proxy"]["enabled"]:
        codex_address = f"http://127.0.0.1:{network['codex_proxy']['port']}"
    service = _service_manager(root)
    try:
        service_state = service.status()
    except RuntimeError as exc:
        service_state = {"active": False, "error": str(exc)}
    result = status(root)
    version_path = Path(__file__).resolve().parent / "VERSION"
    result.update(
        version=version_path.read_text(encoding="utf-8").strip() if version_path.is_file() else "source",
        python=sys.version.split()[0],
        private_root=root_mode == 0o700,
        root_mode=oct(root_mode) if root_mode is not None else None,
        root_disk=_disk_status(root),
        projects=_project_health(root),
        gpu=_gpu_status(),
        service=service_state,
        linger=service.linger_status(),
        network=network,
        proxy_reachability={
            "broker": _proxy_reachable(network["broker_proxy"]["address"]),
            "codex": _proxy_reachable(codex_address),
        },
    )
    components = [
        component(
            "storage",
            "healthy" if result["private_root"] else "degraded",
            HealthCode.OK if result["private_root"] else HealthCode.STORAGE_ERROR,
            "数据目录权限已收紧" if result["private_root"] else "数据目录权限需要检查",
            False,
        ),
        component(
            "receiver-service",
            "healthy" if bool(service_state.get("active")) else "degraded",
            HealthCode.OK if bool(service_state.get("active")) else HealthCode.OFFLINE,
            "Linux 接收服务正在运行" if service_state.get("active") else "Linux 接收服务未运行",
            not bool(service_state.get("active")),
        ),
    ]
    for index, receiver in enumerate(result["receivers"], 1):
        running = bool(receiver.get("running"))
        components.append(
            component(
                f"receiver:{index}",
                "healthy" if running else "degraded",
                HealthCode.OK if running else HealthCode.OFFLINE,
                "接收进程正在运行" if running else "接收进程未运行",
                not running,
            )
        )
        codex = receiver.get("codex", {})
        if not codex.get("found"):
            codex_state, codex_code, codex_message = "degraded", HealthCode.CLI_NOT_FOUND, "未找到 Codex CLI"
        elif not codex.get("logged_in"):
            codex_state, codex_code, codex_message = (
                "degraded",
                HealthCode.CLI_NOT_LOGGED_IN,
                "Codex CLI 尚未登录",
            )
        else:
            codex_state, codex_code, codex_message = "healthy", HealthCode.OK, "Codex CLI 可用"
        components.append(
            component(
                f"codex:{index}",
                codex_state,
                codex_code,
                codex_message,
                codex_code != HealthCode.OK,
            )
        )
    for name, reachable in result["proxy_reachability"].items():
        if reachable is not False:
            continue
        components.append(
            component(
                "proxy:" + name,
                "degraded",
                HealthCode.PROXY_UNREACHABLE,
                "已配置的本机代理无法连接",
                True,
            )
        )
    result["health"] = report(components)
    return result


def _service_manager(root):
    script = None if getattr(sys, "frozen", False) else Path(__file__).resolve()
    return ServiceManager(root, sys.executable, script)


def service_command(root, args):
    manager = _service_manager(root)
    if args.service_action == "install":
        refresh_mcp_launchers(root)
        result = manager.install()
        result["linger"] = manager.linger_status()
        return result
    if args.service_action == "start":
        manager.start()
        return manager.status()
    if args.service_action == "stop":
        manager.stop()
        return manager.status()
    if args.service_action == "restart":
        refresh_mcp_launchers(root)
        manager.restart()
        return manager.status()
    if args.service_action == "status":
        result = manager.status()
        result["linger"] = manager.linger_status()
        return result
    if args.service_action == "logs":
        if args.follow:
            print(
                "正在跟随 systemd journal；接收端文件日志请另开终端运行不带 --follow 的命令。",
                flush=True,
            )
        completed = manager.logs(args.lines, args.follow)
        if not args.follow:
            if completed.stdout:
                print(completed.stdout, end="")
            if completed.returncode:
                raise RuntimeError((completed.stderr or "journalctl 失败").strip())
            for item in manager.receiver_logs(args.lines):
                print(f"\n== {item['receiver']} receiver logs ==")
                for key in ("stdout", "stderr"):
                    if item.get(key):
                        print(f"-- {key} --")
                        print(item[key])
        return None
    return manager.uninstall()


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.home() / ".local/share/AgentHarness/data")
    commands = parser.add_subparsers(dest="command", required=True)
    pairing = commands.add_parser("link", help="Link this server to your existing Harness account")
    pairing.add_argument("--server", required=True)
    pairing.add_argument("--name", default=socket.gethostname() + " · Linux")
    pairing.add_argument("--codex", help="Absolute Codex CLI path, including a VS Code bundled CLI")
    pairing.add_argument("--broker-proxy", help="Explicit local HTTP proxy, e.g. userspace Tailscale")
    pairing.add_argument(
        "--no-password",
        action="store_true",
        help="Use OS file permissions for unattended identity",
    )
    commands.add_parser("status")
    commands.add_parser("doctor")
    commands.add_parser("diagnostics", help="Print an allowlist-only support snapshot safe to share")
    commands.add_parser("run", help="Foreground supervisor; prefer `service install` for persistence")
    commands.add_parser("stop")
    upgrade = commands.add_parser("upgrade", help="Install a verified Linux receiver bundle side by side")
    upgrade.add_argument("--bundle", type=Path, required=True)
    upgrade.add_argument("--sha256", required=True)
    upgrade.add_argument(
        "--retry",
        action="store_true",
        help="Retry activation of an already installed verified release",
    )
    commands.add_parser("rollback", help="Return to the previous installed Linux receiver")
    internal_mcp = commands.add_parser("mcp-stdio", help="Internal MCP bridge; managed automatically")
    internal_mcp.add_argument("--grant", required=True)
    internal_mcp.add_argument("--network")
    codex = commands.add_parser("codex", help="Connect the interactive Codex CLI to Harness")
    codex_actions = codex.add_subparsers(dest="codex_action", required=True)
    for name in ("connect", "status", "disconnect"):
        codex_actions.add_parser(name)
    codex_path = codex_actions.add_parser("path", help="Manage the Codex CLI executable")
    path_actions = codex_path.add_subparsers(dest="path_action", required=True)
    path_set = path_actions.add_parser("set")
    path_set.add_argument("path")
    path_actions.add_parser("detect")
    path_actions.add_parser("status")
    service = commands.add_parser("service", help="Manage the systemd user service")
    service_actions = service.add_subparsers(dest="service_action", required=True)
    for name in ("install", "start", "stop", "restart", "status", "uninstall"):
        service_actions.add_parser(name)
    logs = service_actions.add_parser("logs")
    logs.add_argument("--lines", type=int, default=100)
    logs.add_argument("--follow", action="store_true")
    network = commands.add_parser("network", help="Configure collaboration and Codex proxies")
    network_actions = network.add_subparsers(dest="network_action", required=True)
    network_actions.add_parser("status")
    network_actions.add_parser("test")
    broker = network_actions.add_parser("broker-proxy")
    broker_actions = broker.add_subparsers(dest="proxy_action", required=True)
    broker_set = broker_actions.add_parser("set")
    broker_set.add_argument("address")
    broker_actions.add_parser("clear")
    codex_proxy = network_actions.add_parser("codex-proxy")
    codex_proxy_actions = codex_proxy.add_subparsers(dest="proxy_action", required=True)
    codex_set = codex_proxy_actions.add_parser("set")
    codex_set.add_argument("--port", type=int, required=True)
    codex_proxy_actions.add_parser("clear")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise PermissionError("拒绝以 root 运行 Agent Harness；请使用专用普通用户")
    args.root = args.root.expanduser().resolve()
    args.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    args.root.chmod(0o700)
    result = None
    if args.command == "link":
        link(args)
    elif args.command == "status":
        result = status(args.root)
    elif args.command == "doctor":
        result = doctor(args.root)
    elif args.command == "diagnostics":
        from agent_harness.diagnostics import build_support_snapshot, validate_public_snapshot

        detailed = doctor(args.root)
        result = build_support_snapshot(
            detailed["health"],
            app_version=detailed["version"],
            capabilities={"linux_receiver": True, "event_cursor": True},
            counts={"receivers": len(detailed["receivers"])},
        )
        validate_public_snapshot(result)
    elif args.command == "run":
        run(args.root)
    elif args.command == "stop":
        write_json(args.root / "linux-stop.json", {"stop": True})
        for path in receiver_paths(args.root):
            DesktopRuntime(path).stop_and_wait()
        result = {"stopping": True}
    elif args.command == "upgrade":
        from agent_harness.linux_upgrade import upgrade

        result = upgrade(args.root, args.bundle, args.sha256, refresh_mcp_launchers, retry=args.retry)
    elif args.command == "rollback":
        from agent_harness.linux_upgrade import rollback

        result = rollback(args.root, refresh_mcp_launchers)
    elif args.command == "mcp-stdio":
        from agent_harness.mcp_adapter import main as mcp_main

        forwarded = ["--grant", args.grant]
        if args.network:
            forwarded.extend(["--network", args.network])
        mcp_main(forwarded)
        return
    elif args.command == "codex":
        if args.codex_action == "path":
            result = codex_path_command(args.root, args.path_action, getattr(args, "path", None))
        else:
            result = codex_command(args.root, args.codex_action)
    elif args.command == "service":
        result = service_command(args.root, args)
    elif args.network_action in {"status", "test"}:
        result = doctor(args.root) if args.network_action == "test" else network_status(args.root)
    elif args.network_action == "broker-proxy":
        result = _set_broker_proxy(args.root, args.address if args.proxy_action == "set" else None)
    else:
        setting = {
            "enabled": args.proxy_action == "set",
            "port": args.port if args.proxy_action == "set" else None,
        }
        result = _set_codex_proxy(args.root, setting)
    if result is not None:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    os.umask(0o077)
    try:
        main()
    except (OSError, RuntimeError, ValueError, PermissionError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
