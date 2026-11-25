"""Desktop controls for an independent, locally owned worker service."""

import json
import hashlib
import os
import subprocess
import sys
import threading
import time
import shutil
import urllib.request
import urllib.error
from pathlib import Path

from agent_harness.common import lock, read_json, rotate_log, write_json
from agent_harness.pull_worker import NoRedirect, TERMINAL, deploy_runtime
from agent_harness.release_info import version_path

SERVICE_RUNTIME_FILES = (
    "agent_attachments.py",
    "broker_transport.py",
    "attachments.py",
    "desktop_service.py",
    "pull_worker.py",
    "common.py",
    "worker.py",
    "warm_worker.py",
    "jobrunner.py",
    "appserver_client.py",
    "release_info.py",
    "owner_worker.py",
    "owner_backend.py",
    "account_identity.py",
    "VERSION",
)


def reconcile_orphaned_turns(root):
    """Close non-terminal model jobs that no project ownership record can reach."""
    root = Path(root)
    with lock(root / "registry.lock"):
        owned = {
            record["job"]
            for path in (root / "projects").glob("*/active.json")
            if (record := read_json(path)) and record.get("job")
        }
        for path in (root / "jobs").glob("*/state.json"):
            state = read_json(path, {})
            if state.get("status") in {"starting", "running", "launching", "unknown"} and state.get("job") not in owned:
                state.update(
                    status="interrupted",
                    finished_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    error="No active worker owns this non-terminal job; start a new turn explicitly",
                )
                write_json(path, state)


def assets():
    if getattr(sys, "frozen", False):
        from agent_harness.service_install import installed_bundle

        client = installed_bundle() / "client"
        package = client / "agent_harness"
        return package if package.is_dir() else client
    return Path(__file__).resolve().parent


def web_assets():
    """Load UI resources from the same verified bundle as the receiver."""
    root = assets()
    if getattr(sys, "frozen", False) and not (root / "web" / "app.html").is_file():
        raise RuntimeError("应用安装不完整：缺少界面资源")
    return root / "web" if (root / "web").is_dir() else root.parents[1] / "assets" / "web"


def python_command():
    if getattr(sys, "frozen", False):
        from agent_harness.service_install import installed_bundle, runtime_python

        return str(runtime_python(installed_bundle()))
    return sys.executable


class DesktopRuntime:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.config_path = self.root / "pull-config.json"
        self.control_path = self.root / "desktop-service.json"

    def _control(self, operation, payload=None):
        info = read_json(self.control_path, {})
        if not info:
            raise RuntimeError("请先连接这台机器")
        from urllib.parse import urlsplit

        address = urlsplit(info["url"])
        if (
            address.scheme != "http"
            or address.hostname != "127.0.0.1"
            or not address.port
            or address.path
            or address.query
            or address.fragment
            or address.username
        ):
            raise RuntimeError("本机服务地址无效")
        request = urllib.request.Request(
            info["url"] + "/desktop/" + operation,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={
                "X-Desktop-Key": info["key"],
                "Origin": info["url"],
                "Content-Type": "application/json",
            },
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        try:
            with opener.open(
                request,
                timeout=45 if payload is not None and operation == "action" else 3,
            ) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                error = json.load(exc).get("error", "本机操作失败")
            except (ValueError, AttributeError):
                error = "本机操作失败"
            raise RuntimeError(error) from None
        if not result.get("ok"):
            raise RuntimeError(result.get("error", "本机操作失败"))
        if result.get("instance") != info["instance"]:
            raise RuntimeError("本机服务身份已改变，请重新打开应用")
        return result.get("result", {})

    def status(self):
        config = read_json(self.config_path, {})
        value = {
            "paired": bool(config),
            "running": False,
            "online": False,
            "node": config.get("node", ""),
            "server": config.get("server", ""),
        }
        try:
            value.update(self._control("status"))
        except (OSError, ValueError, RuntimeError):
            pass
        return value

    def pair(self, server, code, broker_proxy=None):
        if self.config_path.exists():
            raise ValueError("这台机器已配对，请保留原身份并点启动")
        if not code.strip():
            raise ValueError("请填写一次性配对码")
        self.root.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [
                python_command(),
                "-X",
                "utf8",
                str(assets() / "desktop_service.py"),
                "pair",
                str(self.root),
            ],
            input=json.dumps(
                {
                    "server": server.strip(),
                    "code": code.strip(),
                    "broker_proxy": broker_proxy,
                }
            ),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=45,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            value = json.loads(result.stdout)
        except ValueError:
            raise RuntimeError("配对程序无法运行，请检查安装是否完整") from None
        if not value.get("ok"):
            raise RuntimeError(value.get("error", "配对失败"))
        return value.get("result")

    def _service_snapshot(self):
        """Create/reuse an immutable code directory for one receiver version."""
        source = assets()

        def source_file(name):
            path = source / name
            if name == "VERSION" and not path.is_file():
                path = version_path()
                if path is None:
                    raise FileNotFoundError("Application VERSION is missing")
            return path

        digest = hashlib.sha256()
        if getattr(sys, "frozen", False):
            from agent_harness.service_install import installed_bundle

            bundle = installed_bundle()
            digest.update((bundle / ".complete").read_bytes())
            # A repaired generation has a different executable and import path
            # even when the package contents match the original build digest.
            digest.update(bundle.name.encode("utf-8"))
        else:
            for module in sorted(source.glob("*.py")):
                digest.update(module.name.encode("utf-8"))
                digest.update(hashlib.sha256(module.read_bytes()).digest())
        for name in SERVICE_RUNTIME_FILES:
            path = source_file(name)
            digest.update(name.encode())
            digest.update(hashlib.sha256(path.read_bytes()).digest() if path.is_file() else b"")
        snapshot = self.root / "service-runtime" / digest.hexdigest()[:16]
        if not snapshot.is_dir():
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            temporary = snapshot.with_name(snapshot.name + ".tmp-" + str(os.getpid()))
            if temporary.exists():
                shutil.rmtree(temporary)
            temporary.mkdir()
            for name in SERVICE_RUNTIME_FILES:
                item = source_file(name)
                if item.is_file():
                    shutil.copyfile(item, temporary / name)
            try:
                os.replace(temporary, snapshot)
            except FileExistsError:
                shutil.rmtree(temporary)
        return snapshot

    def _has_work(self):
        if any(read_json(path) for path in (self.root / "projects").glob("*/active.json")):
            return True
        return any(
            read_json(path, {}).get("status") not in TERMINAL - {"unknown"}
            for pattern in ("jobs/*/state.json", "projects/*/executions/*/state.json")
            for path in self.root.glob(pattern)
        )

    def start(self, *, refresh_only=False):
        if not self.config_path.exists():
            if refresh_only:
                return {"running": False}
            raise ValueError("请先填写地址和配对码，连接这台机器")
        self.root.mkdir(parents=True, exist_ok=True)
        with lock(self.root / "desktop-start.lock"):
            existing = self.status()
            if refresh_only and (not existing["running"] or existing.get("stopping")):
                return existing
            if existing.get("stopping"):
                raise RuntimeError("正在停止接收，请稍后再启动")
            snapshot = self._service_snapshot()
            if existing["running"]:
                info = read_json(self.control_path, {})
                running_assets = info.get("assets")
                if running_assets == str(snapshot) or (not refresh_only and running_assets == str(assets())):
                    return existing
                if self._has_work():
                    return {**existing, "refresh_pending": True}
                try:
                    if not self._control("stop-if-idle", {}).get("stopping"):
                        return {**existing, "refresh_pending": True}
                except RuntimeError as exc:
                    if str(exc) != "Unknown desktop operation":
                        raise
                    # Earlier observers already finish their current cycle and
                    # leave active child processes alone during graceful stop.
                    if self._has_work():
                        return {**existing, "refresh_pending": True}
                self.stop_and_wait()
            # A paired receiver keeps its identity and project state across
            # app upgrades, but its executable Python files must come from the
            # currently installed bundle before the new service starts.
            reconcile_orphaned_turns(self.root)
            if not self._has_work():
                deploy_runtime(self.root, assets())
            # Every receiver generation runs from an immutable snapshot. This
            # permits the observer service to restart while an old warm worker
            # is active, without overwriting files that process may use.
            flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS if os.name == "nt" else 0
            stdout_log = self.root / "desktop-service.stdout.log"
            stderr_log = self.root / "desktop-service.stderr.log"
            rotate_log(stdout_log)
            rotate_log(stderr_log)
            with stdout_log.open("ab") as out, stderr_log.open("ab") as err:
                process = subprocess.Popen(
                    [
                        python_command(),
                        "-X",
                        "utf8",
                        str(snapshot / "desktop_service.py"),
                        "serve",
                        str(self.root),
                    ],
                    cwd=snapshot,
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=err,
                    creationflags=flags,
                    start_new_session=os.name != "nt",
                )
            threading.Thread(target=process.wait, daemon=True).start()
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                status = self.status()
                if status["running"]:
                    return status
                if process.poll() is not None:
                    raise RuntimeError(
                        "后台接收服务启动失败。请保留已有账户，查看 desktop-service.stderr.log 启动日志；不要重复创建账户"
                    )
                time.sleep(0.15)
            raise RuntimeError("服务还未就绪。若原执行端仍在运行，请先停止它；不要重新配对")

    def stop(self):
        if self.status()["running"]:
            self._control("stop", {})
        return {"stopping": True}

    def stop_and_wait(self, timeout=60):
        self.stop()
        deadline = time.monotonic() + timeout
        while self.status()["running"]:
            if time.monotonic() >= deadline:
                raise RuntimeError("后台服务仍在完成当前操作，窗口将保留。请稍后重试完全退出。")
            time.sleep(0.1)
        with lock(self.root / "pull-worker.lock"):
            pass
        return {"stopped": True}

    def start_workbench(self, open_browser=True, state_root=None):
        """Host an ordinary loopback workbench, without changing network access."""
        url = "http://127.0.0.1:8765"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

        def ready():
            try:
                with opener.open(url + "/healthz", timeout=1) as response:
                    value = json.load(response)
                return value.get("ok") is True and value.get("service") == "foolship-hub"
            except (OSError, ValueError):
                return False

        if not ready():
            if not state_root:
                raise ValueError("A team-service state directory is required")
            state = Path(state_root).expanduser().resolve()
            state.mkdir(parents=True, exist_ok=True)
            with lock(state / "desktop-host-start.lock"):
                if not ready():
                    flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS if os.name == "nt" else 0
                    stdout_log = state / "panel.stdout.log"
                    stderr_log = state / "panel.stderr.log"
                    rotate_log(stdout_log)
                    rotate_log(stderr_log)
                    with stdout_log.open("ab") as out, stderr_log.open("ab") as err:
                        process = subprocess.Popen(
                            [
                                python_command(),
                                "-X",
                                "utf8",
                                str(assets() / "panel.py"),
                                "--state",
                                str(state),
                                "--desktop-control",
                            ],
                            cwd=assets(),
                            stdin=subprocess.DEVNULL,
                            stdout=out,
                            stderr=err,
                            creationflags=flags,
                            start_new_session=os.name != "nt",
                        )
                    threading.Thread(target=process.wait, daemon=True).start()
                    deadline = time.monotonic() + 8
                    while not ready():
                        if process.poll() is not None or time.monotonic() > deadline:
                            raise RuntimeError("无法启动工作台，8765 端口可能被其他软件使用")
                        time.sleep(0.15)
        if open_browser:
            import webbrowser

            webbrowser.open(url)
        return url
