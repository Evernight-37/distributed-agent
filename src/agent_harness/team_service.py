"""Local Tailscale Serve controls. Never overwrite an unrelated 443 listener."""

import argparse
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from common import lock, read_json, write_json
from desktop_runtime import assets, python_command

TARGET = "http://127.0.0.1:8765"
AUTH_URL = re.compile(r"https://login\.tailscale\.com/f/serve\?node=[A-Za-z0-9_-]+")


def executable():
    path = shutil.which("tailscale.exe" if os.name == "nt" else "tailscale")
    fallback = (
        Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale")
        if sys.platform == "darwin"
        else Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Tailscale" / "tailscale.exe"
    )
    return path or (str(fallback) if fallback.is_file() else None)


def cli_environment():
    env = dict(os.environ)
    if sys.platform == "darwin":
        # The macOS binary also hosts the GUI. Finder provides no terminal
        # environment, so explicitly select CLI mode for status and Serve.
        env["TAILSCALE_BE_CLI"] = "1"
    return env


def run_cli(args):
    binary = executable()
    if not binary:
        raise FileNotFoundError("未安装 Tailscale，请先安装并登录。")
    result = subprocess.run(
        [binary, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=8,
        env=cli_environment(),
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip()[:800] or "Tailscale 操作失败")
    if args[-1] != "--json":
        return {}
    try:
        value = json.loads(result.stdout)
    except ValueError:
        raise RuntimeError("Tailscale 未返回有效的 JSON 状态，请确认已登录并连接网络，然后重新检查。") from None
    if not isinstance(value, dict):
        raise RuntimeError("Tailscale 返回的状态格式无效，无法确认现有团队入口配置。")
    return value


def inspect_config(config, hostname):
    if not isinstance(config, dict):
        return "conflict"
    if config.get("Foreground"):
        return "conflict"
    tcp = config.get("TCP", {})
    web = config.get("Web", {})
    funnel = config.get("AllowFunnel", {})
    if not all(isinstance(x, dict) for x in (tcp, web, funnel)):
        return "conflict"
    endpoint = hostname + ":443"
    listener = tcp.get("443")
    matches = {k: v for k, v in web.items() if k.endswith(":443")}
    if any(funnel.values()):
        return "conflict"
    if "443" not in tcp and not matches:
        return "disabled"
    if listener != {"HTTPS": True} or set(matches) != {endpoint} or any(funnel.values()):
        return "conflict"
    host = matches[endpoint]
    if not isinstance(host, dict) or host.get("Handlers") != {"/": {"Proxy": TARGET}}:
        return "conflict"
    return "ready"


class TeamService:
    def __init__(self, root, server, runner=run_cli):
        self.root = Path(root)
        self.server = server.rstrip("/")
        self.runner = runner
        self.job = self.root / "team-service-operation.json"

    def _local(self, hostname=""):
        return self.server in {TARGET, "http://localhost:8765"} or (hostname and self.server == "https://" + hostname)

    def status(self):
        if not self._local() and not self.server.startswith("https://"):
            return {
                "state": "remote",
                "can_manage": False,
                "url": self.server,
                "message": "当前账户连接其它机器的服务，请在托管电脑配置团队入口。",
            }
        try:
            state = self.runner(["status", "--json"])
            host = state.get("Self", {}).get("DNSName", "").rstrip(".").lower()
            if not self._local(host):
                return {
                    "state": "remote",
                    "can_manage": False,
                    "url": self.server,
                    "message": "当前服务由其它机器托管。",
                }
            if state.get("BackendState") != "Running":
                return {"state": "needs_login", "can_manage": True, "message": "请先在 Tailscale 中登录并连接网络。"}
            if not re.fullmatch(r"[a-z0-9-]+\.[a-z0-9-]+\.ts\.net", host):
                return {
                    "state": "error",
                    "can_manage": True,
                    "message": "尚未获得有效的 Tailscale HTTPS 域名，请检查 MagicDNS。",
                }
            config = self.runner(["serve", "status", "--json"])
            kind = inspect_config(config, host)
            result = {
                "state": kind,
                "configuration_checked": True,
                "can_manage": True,
                "url": "https://" + host,
                "message": {
                    "ready": "入口已配置。复制地址给获准加入 Tailscale 网络的同事。",
                    "disabled": "尚未创建团队入口。",
                    "conflict": "443 端口有其它 Serve 配置，应用不会覆盖或停止它。",
                }[kind],
            }
            operation = read_json(self.job, {})
            if kind == "disabled" and operation.get("state") in {"starting", "needs_authorization", "error"}:
                if operation.get("state") == "error" or time.time() - operation.get("updated", 0) < 650:
                    result.update(
                        {k: operation[k] for k in ("state", "message", "authorization_url") if k in operation}
                    )
                else:
                    result.update(state="error", message="上次配置未完成，请重新创建入口。")
            return result
        except FileNotFoundError as exc:
            return {"state": "missing", "can_manage": self._local(), "message": str(exc)}
        except Exception as exc:
            return {"state": "error", "can_manage": self._local(), "message": str(exc)}

    def create(self):
        with lock(self.root / "team-service-launch.lock"):
            state = self.status()
            if not state.get("can_manage"):
                raise PermissionError("只能在托管协作服务的电脑创建入口。")
            if not state.get("configuration_checked"):
                raise ValueError(state.get("message", "不能确认现有入口配置"))
            if state["state"] == "ready":
                return state
            if state["state"] in {"conflict", "missing", "needs_login"}:
                raise ValueError(state["message"])
            if state["state"] in {"starting", "needs_authorization"}:
                return state
            write_json(self.job, {"state": "starting", "message": "正在创建团队入口…", "updated": time.time()})
            flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS if os.name == "nt" else 0
            try:
                process = subprocess.Popen(
                    [
                        python_command(),
                        "-X",
                        "utf8",
                        str(assets() / "team_service.py"),
                        "--create",
                        "--root",
                        str(self.root),
                        "--server",
                        self.server,
                    ],
                    cwd=assets(),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=flags,
                    start_new_session=os.name != "nt",
                )
                threading.Thread(target=process.wait, daemon=True).start()
            except Exception:
                write_json(
                    self.job,
                    {"state": "error", "message": "配置程序未启动，请检查应用是否完整。", "updated": time.time()},
                )
                raise
            return dict(state, state="starting", message="正在创建团队入口…")

    def stop(self):
        with lock(self.root / "team-service-control.lock"):
            state = self.status()
            if not state.get("can_manage") or state["state"] not in {"ready", "disabled"}:
                raise PermissionError("入口正在配置或属于其它服务，不能停止。")
            if state["state"] == "ready":
                self.runner(["serve", "--bg", "--https=443", "off"])
            write_json(self.job, {"state": "disabled", "updated": time.time()})
            return self.status()

    def authorize(self):
        state = self.status()
        url = state.get("authorization_url", "")
        if state["state"] != "needs_authorization" or not AUTH_URL.fullmatch(url):
            raise ValueError("当前没有待确认的 Tailscale 授权。")
        webbrowser.open(url)
        return state

    def copy(self):
        state = self.status()
        url = state.get("url", "")
        if state["state"] not in {"ready", "remote"} or not url:
            raise ValueError("团队入口尚未配置。")
        if state["state"] == "remote" and not url.startswith("https://"):
            raise ValueError("这是本机转发地址，不能交给其它电脑；请向托管者获取 HTTPS 团队地址。")
        from pull_worker import validate_server

        validate_server(url)
        if sys.platform == "darwin":
            proc = subprocess.run(["/usr/bin/pbcopy"], input=url, capture_output=True, text=True, timeout=5)
            if proc.returncode:
                raise RuntimeError("复制失败，可直接选中地址复制。")
            return {"copied": True, "message": "服务地址已复制。"}
        if os.name != "nt":
            raise ValueError("当前复制功能仅支持 Windows。")
        powershell = str(
            Path(os.environ.get("SystemRoot", "C:/Windows"))
            / "System32"
            / "WindowsPowerShell"
            / "v1.0"
            / "powershell.exe"
        )
        proc = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "$address = [Console]::In.ReadToEnd(); Set-Clipboard -Value $address",
            ],
            input=url,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if proc.returncode:
            raise RuntimeError("复制失败，可直接选中地址复制。")
        return {"copied": True, "message": "服务地址已复制。"}


def create_background(root, server):
    manager = TeamService(root, server)
    process = None
    try:
        with lock(manager.root / "team-service-control.lock"):
            state = manager.status()
            if (
                not state.get("configuration_checked")
                or not state.get("can_manage")
                or state["state"] in {"conflict", "missing", "needs_login"}
            ):
                raise ValueError(state.get("message", "不能配置此入口"))
            if state["state"] == "ready":
                write_json(manager.job, dict(state, updated=time.time()))
                return
            binary = executable()
            if not binary:
                raise FileNotFoundError("未安装 Tailscale。")
            process = subprocess.Popen(
                [binary, "serve", "--bg", "--https=443", TARGET],
                env=cli_environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            lines = queue.Queue()

            def read_lines():
                for line in process.stdout:
                    lines.put(line)

            threading.Thread(target=read_lines, daemon=True).start()
            deadline = time.monotonic() + 600
            current = {"state": "starting", "message": "正在配置 HTTPS 团队入口…"}
            output = ""
            while True:
                while not lines.empty():
                    line = lines.get()
                    output = (output + line)[-3000:]
                    authorization_match = AUTH_URL.search(line)
                    if authorization_match:
                        current = {
                            "state": "needs_authorization",
                            "authorization_url": authorization_match.group(),
                            "message": "需要在 Tailscale 页面确认启用 Serve / HTTPS。",
                        }
                write_json(manager.job, dict(current, updated=time.time()))
                if process.poll() is not None:
                    break
                if time.monotonic() > deadline:
                    raise TimeoutError("授权等待已超时，请重新创建入口。")
                time.sleep(0.3)
            if process.returncode:
                raise RuntimeError(output.strip()[:800] or "Tailscale 配置失败")
            final = manager.status()
            if final["state"] != "ready":
                raise RuntimeError("配置命令已结束，但未发现预期入口。请检测状态。")
            write_json(manager.job, dict(final, updated=time.time()))
    except Exception as exc:
        write_json(manager.job, {"state": "error", "message": str(exc), "updated": time.time()})
    finally:
        if process and process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--create", action="store_true")
    parser.add_argument("--root", required=True)
    parser.add_argument("--server", required=True)
    args = parser.parse_args()
    if args.create:
        create_background(args.root, args.server)
