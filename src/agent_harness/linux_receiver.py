"""Headless account linking and receiver supervision; no GUI dependencies."""

import argparse
import getpass
import json
import os
import signal
import socket
import sys
import threading
import time
from pathlib import Path

from app_bridge import AppBridge
from broker_transport import broker_opener
from common import lock, write_json
from desktop_runtime import DesktopRuntime
from worker import cli_status, codex_version


def invoke(bridge, action, payload=None):
    value = bridge.invoke(action, payload)
    if not value.get("ok"):
        raise RuntimeError(value.get("error", action + " failed"))
    return value["result"]


def receiver_paths(root):
    return sorted(path for path in (Path(root) / "receivers").glob("device-*") if (path / "pull-config.json").is_file())


def status(root):
    bridge = AppBridge(root)
    paths = receiver_paths(root)
    return {
        "root": str(bridge.root),
        "profiles": invoke(bridge, "local-status")["profiles"],
        "codex": cli_status(paths[0] if paths else bridge.root),
        "receivers": [DesktopRuntime(path).status() for path in paths],
    }


def link(args):
    bridge = AppBridge(args.root)
    if bridge._profiles():
        raise ValueError("这台接收端已有账户，请使用 status 查看；不要重复关联")
    if args.broker_proxy:
        broker_opener(args.broker_proxy)
        write_json(bridge.root / "broker-network.json", {"proxy": args.broker_proxy})
        (bridge.root / "broker-network.json").chmod(0o600)
        bridge.broker_proxy = args.broker_proxy
    if args.codex:
        path = Path(args.codex).expanduser().resolve()
        if not path.is_file() or not codex_version(path):
            raise ValueError("请选择可运行的 Codex CLI")
        write_json(bridge.root / "codex-cli.json", {"path": str(path), "mode": "manual"})
    password = "" if args.no_password else getpass.getpass("为服务器上的本机身份设置解锁密码：")
    request = invoke(bridge, "device-request", {"server": args.server, "name": args.name, "password": password})
    print("在 Mac Harness 的账户页点击“输入新电脑的关联码”，核对并批准这台 Linux 服务器：", flush=True)
    print(request["code"], flush=True)
    print("设备名称：" + args.name, flush=True)
    deadline = time.monotonic() + min(request.get("expires_in", 600), 600)
    while time.monotonic() < deadline:
        result = invoke(bridge, "device-claim")
        if result.get("status") == "approved":
            print("关联成功。可在 Mac 中为这台服务器添加员工、选择服务器项目目录并派入项目。", flush=True)
            return
        time.sleep(2)
    raise RuntimeError("关联码已过期，请重新执行 link")


def run(root):
    paths = receiver_paths(root)
    if not paths:
        raise ValueError("请先执行 link 并在 Mac 上批准关联")
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.home() / ".local/share/AgentHarness/data")
    commands = parser.add_subparsers(dest="command", required=True)
    pairing = commands.add_parser("link", help="Link this server to your existing Harness account")
    pairing.add_argument("--server", required=True)
    pairing.add_argument("--name", default=socket.gethostname() + " · Linux")
    pairing.add_argument("--codex", help="Absolute Codex CLI path, including a VS Code bundled CLI")
    pairing.add_argument("--broker-proxy", help="Explicit local HTTP proxy, e.g. userspace Tailscale")
    pairing.add_argument(
        "--no-password", action="store_true", help="Use OS file permissions for the unattended identity"
    )
    commands.add_parser("status")
    commands.add_parser("run", help="Foreground supervisor; use a user service or tmux for persistence")
    commands.add_parser("stop")
    args = parser.parse_args(argv)
    args.root = args.root.expanduser().resolve()
    args.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    args.root.chmod(0o700)
    if args.command == "link":
        link(args)
    elif args.command == "status":
        print(json.dumps(status(args.root), ensure_ascii=False, indent=2))
    elif args.command == "run":
        run(args.root)
    else:
        write_json(args.root / "linux-stop.json", {"stop": True})
        for path in receiver_paths(args.root):
            DesktopRuntime(path).stop_and_wait()
        print("已请求停止后台接收服务。")


if __name__ == "__main__":
    os.umask(0o077)
    try:
        main()
    except (OSError, RuntimeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
