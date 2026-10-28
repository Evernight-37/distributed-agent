"""Desktop control boundary independent from the native UI implementation."""

import secrets
import threading
import time
from pathlib import Path

from agent_harness.api_contracts import DESKTOP_CONTROL
from agent_harness.common import read_json, validate_local_proxy, write_json
from agent_harness.desktop_runtime import DesktopRuntime


class DesktopControlAPI:
    def __init__(self, handler, action_groups=()):
        self.handler = handler
        self.action_groups = tuple(action_groups)

    def invoke(self, action, payload=None):
        action, payload = DESKTOP_CONTROL.validate(action, payload)
        for group in self.action_groups:
            if action in group.actions:
                return group.invoke(action, payload)
        return self.handler(action, payload)

    @staticmethod
    def contract():
        return DESKTOP_CONTROL.describe()


class DesktopSessionActions:
    """Small desktop-process/session actions kept outside the main bridge router."""

    actions = frozenset(
        {
            "app-services",
            "app-exit",
            "app-background",
            "diagnostics-get",
            "health-status",
            "local-status",
            "logout",
        }
    )

    def __init__(self, bridge):
        self.bridge = bridge

    def invoke(self, action, payload):
        if action in {"app-services", "health-status"}:
            from agent_harness.app_lifecycle import Lifecycle

            status = Lifecycle(self.bridge.root, host_root=self.bridge._host_state_root()).status()
            return status["health"] if action == "health-status" else status
        if action == "diagnostics-get":
            from agent_harness import __version__
            from agent_harness.app_lifecycle import Lifecycle
            from agent_harness.diagnostics import build_support_snapshot, validate_public_snapshot

            status = Lifecycle(self.bridge.root, host_root=self.bridge._host_state_root()).status()
            result = build_support_snapshot(
                status["health"],
                app_version=__version__,
                capabilities={"desktop": True, "event_cursor": True},
                counts={"receivers": len(status["receivers"])},
            )
            validate_public_snapshot(result)
            return result
        if action in {"app-exit", "app-background"}:
            if action == "app-exit":
                from agent_harness.app_lifecycle import Lifecycle

                result = Lifecycle(self.bridge.root, host_root=self.bridge._host_state_root()).stop()
            else:
                result = {"background": True}
            self.bridge.allow_window_close = True
            if self.bridge.window:
                threading.Timer(0.3, self.bridge.window.destroy).start()
            return result
        if action == "local-status":
            return {
                "profiles": [
                    {"id": profile["id"], "username": profile["username"]} for profile in self.bridge._profiles()
                ],
                "unlocked": bool(self.bridge.identity),
                "server": (self.bridge.profile or {}).get("server", "http://127.0.0.1:8765"),
            }
        self.bridge.identity = None
        self.bridge.profile = None
        self.bridge.activated_services = set()
        return {}


class DesktopSettingsActions:
    """Local Codex, proxy, and application settings owned by this desktop."""

    actions = frozenset(
        {
            "settings-get",
            "codex-mcp-status",
            "codex-mcp-enable",
            "codex-mcp-disable",
            "codex-cli-status",
            "codex-cli-redetect",
            "codex-cli-choose",
            "codex-cli-save",
            "proxy-save",
            "warm-idle-save",
            "settings-save",
        }
    )

    def __init__(self, bridge):
        self.bridge = bridge

    def _require_login(self):
        if not self.bridge.profile or not self.bridge.identity:
            raise PermissionError("请先登录")

    def _receiver_paths(self):
        receivers = self.bridge.root / "receivers"
        return [
            path
            for path in receivers.glob("device-*")
            if path.is_dir() and path.resolve().parent == receivers.resolve()
        ]

    def _cli_status(self):
        from agent_harness.worker import cli_status, codex_login_status

        result = cli_status(self.bridge._runtime().root)
        if result.get("found"):
            result.update(codex_login_status(result["path"], self.bridge._runtime().root))
        return result

    def invoke(self, action, data):
        self._require_login()
        if action == "settings-save":
            if "server" in data and data["server"].rstrip("/") != self.bridge.profile["server"]:
                raise ValueError("首个服务地址不可在此修改。加入其他服务请使用项目邀请。")
            if "background" in data:
                self.bridge.profile["background"] = bool(data["background"])
                write_json(
                    self.bridge.root / "profiles" / (self.bridge.profile["id"] + ".json"),
                    self.bridge.profile,
                )
            return {"saved": True}
        if action == "settings-get":
            from agent_harness.team_service import TeamService

            dashboard = self.bridge._dashboard()
            local_owned = any(
                service["server"] == "http://127.0.0.1:8765" and service["hosted_by_me"]
                for service in dashboard["services"]
            )
            enabled = bool(self.bridge.profile.get("local_host_enabled") or local_owned)
            service = TeamService(self.bridge.root, self.bridge._hosting_server())
            status = service.status()
            if enabled:
                self.bridge._publish_hosted_service(status)
            return {
                "server": self.bridge.profile["server"],
                "services": dashboard["services"],
                "can_host_locally": not enabled,
                "local_host_enabled": enabled,
                "background": self.bridge.profile.get("background", True),
                "autostart": False,
                "data_root": str(self.bridge.root),
                "team_service": status,
                "proxy": self.bridge._proxy_settings(),
                "warm_idle_seconds": self.bridge._idle_seconds(),
                "codex_mcp": self.bridge._mcp_status(),
            }
        if action == "codex-mcp-status":
            return self.bridge._mcp_status()
        if action == "codex-mcp-enable":
            return self.bridge._enable_mcp()
        if action == "codex-mcp-disable":
            return self.bridge._disable_mcp()
        if action == "codex-cli-status":
            from agent_harness.worker import codex_login_status

            saved = read_json(self.bridge.root / "codex-cli.json", {})
            if (
                isinstance(saved, dict)
                and isinstance(saved.get("path"), str)
                and saved["path"]
                and Path(saved["path"]).is_file()
            ):
                result = {
                    "found": True,
                    "configured": saved.get("mode") == "manual",
                    "path": saved["path"],
                    "version": saved.get("version", ""),
                }
                result.update(codex_login_status(saved["path"], self.bridge._runtime().root))
                return result
            if isinstance(saved, dict) and saved.get("mode") == "manual":
                return {
                    "found": False,
                    "configured": True,
                    "error": "设置的 Codex 路径已失效，请重新选择程序",
                }
            if isinstance(saved, dict) and saved.get("mode") == "auto" and not saved.get("path"):
                return {
                    "found": False,
                    "configured": False,
                    "error": saved.get("error", "未找到可运行的 Codex CLI；请点击重新检测"),
                }
            result = self._cli_status()
            write_json(
                self.bridge.root / "codex-cli.json",
                {
                    "path": result.get("path", ""),
                    "version": result.get("version", ""),
                    "error": result.get("error", ""),
                    "mode": "auto",
                },
            )
            if result["found"]:
                for receiver in self._receiver_paths():
                    self.bridge._sync_cli(DesktopRuntime(receiver), result["path"])
            return result
        if action == "codex-cli-redetect":
            for receiver in self._receiver_paths():
                self.bridge._sync_cli(DesktopRuntime(receiver), "")
            result = self._cli_status()
            selected = result.get("path", "") if result.get("found") else ""
            for receiver in self._receiver_paths():
                self.bridge._sync_cli(DesktopRuntime(receiver), selected)
            write_json(
                self.bridge.root / "codex-cli.json",
                {
                    "path": selected,
                    "version": result.get("version", ""),
                    "error": result.get("error", ""),
                    "mode": "auto",
                },
            )
            return result
        if action == "codex-cli-choose":
            if not self.bridge.window:
                raise RuntimeError("文件选择器尚未就绪")
            import webview

            chosen = self.bridge.window.create_file_dialog(webview.FileDialog.OPEN)
            return {"path": chosen[0] if chosen else ""}
        if action == "codex-cli-save":
            selected = data.get("path", "")
            if not isinstance(selected, str):
                raise ValueError("Codex 路径必须是文字")
            selected = selected.strip()
            if selected:
                from agent_harness.worker import codex_version

                if not Path(selected).is_absolute() or not Path(selected).is_file() or codex_version(selected) is None:
                    raise ValueError("请选择能运行的 Codex CLI 程序（macOS: codex；Windows: codex.exe 或 codex.cmd）")
            for receiver in self._receiver_paths():
                self.bridge._sync_cli(DesktopRuntime(receiver), selected)
            result = self._cli_status()
            if not selected and result.get("found"):
                selected = result["path"]
                for receiver in self._receiver_paths():
                    self.bridge._sync_cli(DesktopRuntime(receiver), selected)
            write_json(
                self.bridge.root / "codex-cli.json",
                {
                    "path": selected,
                    "version": result.get("version", ""),
                    "error": result.get("error", ""),
                    "mode": "manual" if data.get("path", "").strip() else "auto",
                },
            )
            return result
        if action == "proxy-save":
            setting = validate_local_proxy(data)
            for receiver in self._receiver_paths():
                self.bridge._sync_proxy(DesktopRuntime(receiver), setting)
            write_json(self.bridge.root / "codex-proxy.json", setting)
            return setting
        seconds = data.get("seconds")
        if isinstance(seconds, bool) or not isinstance(seconds, int) or not 30 <= seconds <= 3600:
            raise ValueError("空闲保活时间须为 30 到 3600 秒")
        for receiver in self._receiver_paths():
            self.bridge._sync_idle(DesktopRuntime(receiver), seconds)
        write_json(self.bridge.root / "codex-warm-idle.json", {"seconds": seconds})
        return {"seconds": seconds}


class DesktopTeamServiceActions:
    """Local collaboration-service hosting, backup, restore, and Tailscale entry."""

    actions = frozenset(
        {
            "team-service-authorize",
            "team-service-backup",
            "team-service-copy",
            "team-service-create",
            "team-service-restore-apply",
            "team-service-restore-select",
            "team-service-status",
            "team-service-stop",
            "team-service-test",
        }
    )

    def __init__(self, bridge):
        self.bridge = bridge

    def invoke(self, action, data):
        if not self.bridge.profile or not self.bridge.identity:
            raise PermissionError("请先登录")
        from agent_harness.team_service import TeamService

        service = TeamService(self.bridge.root, self.bridge._hosting_server())
        if action == "team-service-status":
            status = service.status()
            status["can_backup"] = bool(
                self.bridge.identity
                and self.bridge.profile
                and (
                    self.bridge.profile.get("server") == "http://127.0.0.1:8765"
                    or self.bridge.profile.get("local_host_enabled")
                )
            )
            self.bridge._publish_hosted_service(status)
            return status
        if action == "team-service-backup":
            if not self.bridge.window:
                raise RuntimeError("文件保存窗口尚未就绪")
            import webview
            from agent_harness.storage import create_database_backup

            store = self.bridge._owned_local_store()
            filename = time.strftime("AgentHarness-team-backup-%Y%m%d-%H%M%S.sqlite3")
            chosen = self.bridge.window.create_file_dialog(webview.FileDialog.SAVE, save_filename=filename)
            if not chosen:
                return {"saved": False}
            path = Path(chosen if isinstance(chosen, str) else chosen[0])
            result = create_database_backup(store.database, path)
            return {"saved": True, "name": path.name, "size": result["size"]}
        if action == "team-service-restore-select":
            if not self.bridge.window:
                raise RuntimeError("文件选择器尚未就绪")
            import webview
            from agent_harness.storage import inspect_database

            self.bridge._owned_local_store()
            chosen = self.bridge.window.create_file_dialog(
                webview.FileDialog.OPEN, file_types=("SQLite backup (*.sqlite3)",)
            )
            if not chosen:
                self.bridge.pending_restore = None
                return {"selected": False}
            path = Path(chosen if isinstance(chosen, str) else chosen[0]).resolve()
            metadata = inspect_database(path)
            if metadata["owner_account_id"] != self.bridge.identity["account_id"]:
                raise PermissionError("备份属于其他账户，不能恢复到当前协作服务")
            token = secrets.token_urlsafe(24)
            self.bridge.pending_restore = {
                "token": token,
                "path": str(path),
                "sha256": metadata["sha256"],
            }
            return {
                "selected": True,
                "token": token,
                "name": path.name,
                "schema_version": metadata["schema_version"],
                "projects": metadata["projects"],
                "users": metadata["users"],
            }
        if action == "team-service-restore-apply":
            pending = self.bridge.pending_restore
            self.bridge.pending_restore = None
            if not pending or not secrets.compare_digest(str(data.get("token", "")), pending["token"]):
                raise PermissionError("恢复确认已失效，请重新选择备份")
            from agent_harness.app_lifecycle import HostRuntime
            from agent_harness.storage import inspect_database, restore_database

            store = self.bridge._owned_local_store()
            metadata = inspect_database(pending["path"])
            if not secrets.compare_digest(metadata["sha256"], pending["sha256"]):
                raise ValueError("备份文件在确认后发生变化，已取消恢复")
            if metadata["owner_account_id"] != self.bridge.identity["account_id"]:
                raise PermissionError("备份属于其他账户，不能恢复")
            HostRuntime(store.root).stop_and_wait()
            try:
                result = restore_database(store.database, pending["path"], store.root / "backups")
            finally:
                DesktopRuntime(self.bridge.root / "host").start_workbench(open_browser=False, state_root=store.root)
            return {
                "restored": True,
                "safety_backup": Path(result["safety_backup"]).name if result["safety_backup"] else None,
            }
        if action == "team-service-create":
            status = service.status()
            if not status.get("can_manage"):
                raise PermissionError("请在托管协作服务的电脑创建入口。")
            self.bridge._rpc("dashboard", server=self.bridge._hosting_server())
            return service.create()
        if action == "team-service-stop":
            result = service.stop()
            (self.bridge._host_state_root() / "team-service.json").unlink(missing_ok=True)
            return result
        if action == "team-service-authorize":
            return service.authorize()
        if action == "team-service-copy":
            return service.copy()
        status = service.status()
        if status["state"] not in {"ready", "remote"}:
            raise ValueError(status.get("message", "入口尚未配置"))
        self.bridge._rpc("dashboard", server=status["url"])
        return {
            "ok": True,
            "message": "连接成功：当前账户可通过此地址访问协作服务。",
        }
