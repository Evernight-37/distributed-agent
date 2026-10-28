"""Local application lifecycle; never stop an unrelated listener or agent job."""

import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from agent_harness.common import lock, read_json
from agent_harness.desktop_runtime import DesktopRuntime
from agent_harness.health import HealthCode, component, report


def split_windows_command(command):
    import ctypes
    from ctypes import wintypes

    count = ctypes.c_int()
    parse = ctypes.windll.shell32.CommandLineToArgvW
    parse.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    parse.restype = ctypes.POINTER(wintypes.LPWSTR)
    argv = parse(command, ctypes.byref(count))
    if not argv:
        return []
    try:
        return [argv[i] for i in range(count.value)]
    finally:
        free = ctypes.windll.kernel32.LocalFree
        free.argtypes = [ctypes.c_void_p]
        free(argv)


class HostRuntime(DesktopRuntime):
    def __init__(self, root):
        super().__init__(root)
        self.control_path = self.root / "desktop-host.json"

    def stop_and_wait(self, timeout=60):
        self.stop()
        deadline = time.monotonic() + timeout
        while self.status()["running"]:
            if time.monotonic() >= deadline:
                raise RuntimeError("本机协作服务尚未退出，请稍后重试")
            time.sleep(0.1)
        with lock(self.root / "coordinator.lock"):
            pass


class Lifecycle:
    def __init__(self, root, host_root=None):
        self.root = Path(root)
        # Match the host directory selected by the desktop runtime.  The
        # Optional argument lets source-mode callers select an isolated host root.
        self.host_root = Path(host_root).expanduser().resolve() if host_root else (self.root / "team-service").resolve()

    def receivers(self):
        return [DesktopRuntime(path) for path in (self.root / "receivers").glob("device-*") if path.is_dir()]

    def active_jobs(self):
        for runtime in self.receivers():
            for pattern in ("jobs/*/state.json", "projects/*/executions/*/state.json"):
                for path in runtime.root.glob(pattern):
                    if read_json(path, {}).get("status") in {
                        "starting",
                        "running",
                        "unknown",
                        "launching",
                    }:
                        return True
        return False

    def status(self):
        host = HostRuntime(self.host_root).status()
        unmanaged = False
        if not host["running"]:
            try:
                with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                    "http://127.0.0.1:8765/healthz", timeout=0.5
                ) as response:
                    value = json.load(response)
                    unmanaged = (
                        response.status == 200
                        and value.get("ok") is True
                        and value.get("service") == "agent-harness-hub"
                    )
            except (OSError, ValueError):
                pass
        receivers = [{"running": r.status()["running"]} for r in self.receivers()]
        components = [
            component(
                "hub",
                "healthy" if host["running"] or unmanaged else "degraded",
                HealthCode.OK if host["running"] or unmanaged else HealthCode.OFFLINE,
                "本机协作服务正在运行" if host["running"] or unmanaged else "本机协作服务未运行",
                not (host["running"] or unmanaged),
            )
        ]
        components.extend(
            component(
                f"receiver:{index}",
                "healthy" if receiver["running"] else "degraded",
                HealthCode.OK if receiver["running"] else HealthCode.OFFLINE,
                "接收服务正在运行" if receiver["running"] else "接收服务未运行",
                not receiver["running"],
            )
            for index, receiver in enumerate(receivers, 1)
        )
        return {
            "receivers": receivers,
            "host": {
                "running": host["running"] or unmanaged,
                "managed": host["running"],
            },
            "active_jobs": self.active_jobs(),
            "health": report(components),
        }

    def stop(self):
        errors = []
        receiver_paths = sorted(path for path in (self.root / "receivers").glob("device-*") if path.is_dir())

        def stop_receiver(item):
            index, path = item
            try:
                runtime = DesktopRuntime(path)
                runtime.stop_and_wait()
                return None
            except Exception as exc:
                return ("接收服务 " + str(index), exc)

        if receiver_paths:
            # Each runtime has its own bounded wait.  Stop them concurrently so
            # one slow receiver cannot multiply the full-exit delay by the
            # number of paired machines.  map preserves stable error ordering.
            with ThreadPoolExecutor(
                max_workers=min(8, len(receiver_paths)),
                thread_name_prefix="stop-receiver",
            ) as pool:
                errors.extend(error for error in pool.map(stop_receiver, enumerate(receiver_paths, 1)) if error)
        try:
            host = HostRuntime(self.host_root)
            if host.status()["running"]:
                host.stop_and_wait()
        except Exception as exc:
            errors.append(("本机协作服务", exc))
        if errors:
            # Bound both the number and length of external error strings.  The
            # user needs actionable service names without an unbounded process
            # or OS error being injected into the window.
            details = "; ".join(f"{name}: {str(exc).strip()[:240] or type(exc).__name__}" for name, exc in errors[:8])
            omitted = len(errors) - 8
            if omitted:
                details += f"; 另有 {omitted} 个服务未能停止"
            raise RuntimeError("部分后台服务未能完全停止。" + details)
        return {"stopped": True, "jobs_continue": self.active_jobs()}
