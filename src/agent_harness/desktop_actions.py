"""Desktop identity, owner, and project business action groups."""

import hashlib
import json
import secrets
import socket
import sys
import uuid
from pathlib import Path

from agent_harness.account_identity import (
    approve_certificate,
    decrypt_identity,
    generate_identity,
    sign_envelope,
)
from agent_harness.common import read_json
from agent_harness.account_recovery import create_recovery_package, recover_device
from agent_harness.ai_setup import render_linux_setup_prompt
from agent_harness.desktop_runtime import web_assets
from agent_harness.pull_worker import validate_server


class DesktopIdentityActions:
    """Local identity unlock, device association, and host ownership."""

    actions = frozenset(
        {
            "register",
            "login",
            "device-request",
            "device-claim",
            "host-local-enable",
            "recovery-export",
            "recovery-import",
            "linux-setup-prompt",
        }
    )

    def __init__(self, bridge):
        self.bridge = bridge

    def invoke(self, action, data):
        bridge = self.bridge
        if action == "linux-setup-prompt":
            if not bridge.identity:
                raise PermissionError("请先登录")
            server = data["server"].strip().rstrip("/")
            validate_server(server)
            if server not in bridge._services():
                raise ValueError("只能为当前账户已接入的协作服务生成提示词")
            prompt = render_linux_setup_prompt(web_assets() / "linux-ai-setup.md", server)
            return {"server": server, "prompt": prompt}
        if action == "recovery-export":
            if not bridge.identity:
                raise PermissionError("请先登录")
            package = create_recovery_package(bridge.identity, data.get("recovery_password", ""))
            if not bridge.window:
                raise RuntimeError("恢复包只能在桌面应用中导出")
            import webview

            chosen = bridge.window.create_file_dialog(
                webview.FileDialog.SAVE,
                save_filename="AgentHarness-recovery.json",
                file_types=("Agent Harness recovery (*.json)",),
            )
            if not chosen:
                return {"saved": False}
            target = Path(chosen[0] if isinstance(chosen, (list, tuple)) else chosen)
            target.write_text(json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8")
            return {"saved": True, "name": target.name}
        if action == "recovery-import":
            if bridge.identity:
                raise ValueError("请先退出当前账户")
            if not bridge.window:
                raise RuntimeError("恢复包只能在桌面应用中导入")
            import webview

            chosen = bridge.window.create_file_dialog(
                webview.FileDialog.OPEN,
                file_types=("Agent Harness recovery (*.json)",),
            )
            if not chosen:
                return {"imported": False}
            source = Path(chosen[0] if isinstance(chosen, (list, tuple)) else chosen)
            if not source.is_file() or source.stat().st_size > 1024 * 1024:
                raise ValueError("恢复包无效或过大")
            try:
                package = json.loads(source.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                raise ValueError("恢复包不是有效的 JSON 文件") from None
            identity = recover_device(package, data.get("recovery_password", ""))
            server = bridge._server(data.get("server"))
            challenge = bridge._post(server, {"action": "challenge", "public_key": identity["public_key"]})
            body = {
                "domain": "harness-account-request-v1",
                "nonce": challenge["nonce"],
                "operation": "activate-device",
                "payload": {"device_name": socket.gethostname()},
            }
            activated = bridge._post(
                server,
                {"action": "activate-device", "envelope": sign_envelope(identity, body)},
            )
            bridge.identity = identity
            bridge.profile = {
                "id": secrets.token_hex(16),
                "username": activated.get("username", "已恢复账户"),
                "server": server,
                "node": activated["node"],
            }
            bridge.activated_services = {server}
            bridge._save_profile(data.get("password", ""))
            bridge._enroll()
            return {"imported": True}
        if action == "register":
            identity = generate_identity()
            server = bridge._server(data.get("server"))
            store = None
            if server == "http://127.0.0.1:8765":
                store = bridge._local_host_state(public_key=identity["public_key"])
                store.claim_local_host(
                    identity["account_id"],
                    "device-" + hashlib.sha256(identity["public_key"].encode()).hexdigest()[:24],
                )
            challenge = bridge._post(server, {"action": "challenge", "public_key": identity["public_key"]})
            body = {
                "domain": "harness-account-request-v1",
                "nonce": challenge["nonce"],
                "operation": "register",
                "payload": {
                    "name": data["username"],
                    "device_name": socket.gethostname(),
                },
            }
            bridge._post(
                server,
                {"action": "register", "envelope": sign_envelope(identity, body)},
            )
            bridge.identity = identity
            bridge.profile = {
                "id": secrets.token_hex(16),
                "username": data["username"],
                "server": server,
                "node": "device-" + hashlib.sha256(identity["public_key"].encode()).hexdigest()[:24],
                "local_host_enabled": server == "http://127.0.0.1:8765",
            }
            if store:
                bridge.profile["local_host_state"] = str(store.root)
            bridge._save_profile(data.get("password", ""))
            bridge._enroll()
            return {"created": True}
        if action == "login":
            profile = next((p for p in bridge._profiles() if p["id"] == data["profile"]), None)
            if not profile:
                raise ValueError("本地账户不存在")
            identity = decrypt_identity(profile["encrypted"], data.get("password", ""))
            bridge.identity = identity
            bridge.profile = profile
            if profile["encrypted"].get("version") == 1:
                bridge._save_profile(data.get("password", ""))
            if profile["server"] == "http://127.0.0.1:8765":
                try:
                    bridge._server(profile["server"])
                except (OSError, RuntimeError, ValueError):
                    bridge.identity = None
                    bridge.profile = None
                    raise
            bridge.activated_services = {bridge.profile["server"]}
            if (
                getattr(sys, "frozen", False)
                and bridge.profile["server"] == "http://127.0.0.1:8765"
                and not bridge.profile.get("local_host_state")
            ):
                try:
                    store = bridge._local_host_state()
                    bridge.profile["local_host_state"] = str(store.root)
                    bridge._save_public_profile()
                except (OSError, RuntimeError, ValueError, PermissionError):
                    pass
            bridge._dashboard()
            bridge._sync_service_links(force=True)
            for server in bridge._attached_services():
                try:
                    bridge._activate_service(server)
                    bridge._enroll() if server == bridge.profile["server"] else bridge._enroll(server)
                except (OSError, RuntimeError, ValueError):
                    pass
            try:
                bridge._retry_mcp_revocations()
            except (OSError, RuntimeError, ValueError, PermissionError):
                pass
            if bridge._mcp_grant_path().is_file():
                try:
                    bridge._sync_mcp_delegate()
                except (OSError, RuntimeError, ValueError, PermissionError):
                    pass
            return {"unlocked": True}
        if action == "device-request":
            identity = generate_identity()
            server = bridge._server(data.get("server"))
            name = data.get("name") or socket.gethostname()
            result = bridge._post(
                server,
                {
                    "action": "request-device",
                    "public_key": identity["public_key"],
                    "name": name,
                },
            )
            bridge.pending = {
                "identity": identity,
                "server": server,
                "password": data.get("password", ""),
                "request": result,
            }
            return result
        if action == "device-claim":
            if not bridge.pending:
                raise ValueError("本机没有待关联请求")
            pending = bridge.pending
            result = bridge._post(
                pending["server"],
                dict(action="claim-device", **pending["request"]),
            )
            if result.get("status") != "approved":
                return result
            identity = pending["identity"]
            identity.update(account_id=result["account_id"], chain=result["chain"])
            from agent_harness.account_identity import verify_envelope

            verify_envelope(
                sign_envelope(identity, {"claim": True}),
                expected_account=result["account_id"],
            )
            bridge.identity = identity
            bridge.profile = {
                "id": secrets.token_hex(16),
                "username": result["username"],
                "server": pending["server"],
                "node": "device-" + hashlib.sha256(identity["public_key"].encode()).hexdigest()[:24],
            }
            bridge.activated_services = {bridge.profile["server"]}
            bridge._save_profile(pending["password"])
            bridge.pending = None
            bridge._sync_service_links(force=True)
            bridge._enroll()
            return {"status": "approved"}
        if not bridge.identity:
            raise PermissionError("请先登录")
        return bridge._enable_local_host()


class DesktopOwnerActions:
    """Machine-owner device, employee, file picker, and approval actions."""

    actions = frozenset(
        {
            "device-inspect",
            "device-approve",
            "device-revoke",
            "employee-create",
            "employee-delete",
            "employee-configure",
            "employee-dispatch",
            "employee-withdraw",
            "browse-folders",
            "command-status",
            "choose-folder",
            "attachment-save",
        }
    )

    def __init__(self, bridge):
        self.bridge = bridge

    def invoke(self, action, data):
        bridge = self.bridge
        if not bridge.identity:
            raise PermissionError("请先登录")
        if action == "device-inspect":
            request = bridge._rpc("inspect-device", {"code": data["code"]})
            request["public_key_fingerprint"] = hashlib.sha256(request["public_key"].encode()).hexdigest()[:16]
            bridge.inspected[request["id"]] = request
            return request
        if action == "device-approve":
            request = bridge.inspected.get(data["request_id"])
            if not request:
                raise ValueError("关联请求不存在")
            chain = approve_certificate(bridge.identity, request["public_key"])
            result = bridge._rpc("approve-device", {"request_id": request["id"], "chain": chain})
            bridge.inspected.pop(request["id"], None)
            return result
        if action == "device-revoke":
            server = data.get("server") or bridge.profile["server"]
            if server not in bridge._services():
                raise ValueError("未知服务")
            return bridge._rpc("revoke-device", data, server=server)
        if action == "employee-create":
            server = data.get("server") or bridge.profile["server"]
            if server not in bridge._services():
                raise ValueError("请先加入该服务上的项目")
            if data["node"] == bridge.profile["node"] and server not in bridge._attached_services():
                raise ValueError("请先在设置中将本机 agent 接入该协作服务")
            return bridge._owner(
                "create",
                data["node"],
                {"id": "employee-" + secrets.token_hex(16), "name": data["name"]},
                server,
            )
        if action == "employee-delete":
            employee = bridge._employee(data["id"])
            return bridge._owner("delete", employee["node"], {"id": employee["id"]}, employee["server"])
        if action == "employee-configure":
            employee = bridge._employee(data["id"])
            settings = {k: data[k] for k in ("model", "effort", "enabled", "stages", "timeout")}
            settings["strict_worktree"] = bool(data.get("strict_worktree", False))
            settings["remote_path"] = data["path"]
            return bridge._owner(
                "configure",
                employee["node"],
                {"id": data["id"], "name": data["name"], "settings": settings},
                employee["server"],
            )
        if action in {"employee-dispatch", "employee-withdraw"}:
            employee = bridge._employee(data["id"])
            project_server = bridge.project_servers.get(data["project"])
            if project_server != employee["server"]:
                raise ValueError("只能派遣已接入这个项目服务的员工")
            return bridge._owner(
                "dispatch" if action.endswith("dispatch") else "withdraw",
                employee["node"],
                {"id": data["id"], "project_id": data["project"]},
                employee["server"],
            )
        if action == "browse-folders":
            result = bridge._owner("browse", data["node"], {"path": data.get("path")}, data.get("server"))
            return {"request_id": result["id"]}
        if action == "command-status":
            return bridge._rpc("command-status", data, server=bridge.command_servers.get(data["id"]))
        if action == "choose-folder":
            if not bridge.window:
                raise RuntimeError("文件夹选择器尚未就绪")
            import webview

            chosen = bridge.window.create_file_dialog(webview.FileDialog.FOLDER)
            return {"path": chosen[0] if chosen else ""}
        if not bridge.window:
            raise RuntimeError("文件保存窗口尚未就绪")
        from agent_harness.attachments import decode_file, safe_name
        import webview

        server = bridge.project_servers.get(data["project"], bridge.profile["server"])
        file = bridge._rpc("attachment-get", data, server=server, timeout=45)
        chosen = bridge.window.create_file_dialog(webview.FileDialog.SAVE, save_filename=safe_name(file["name"]))
        if not chosen:
            return {"saved": False}
        destination = Path(chosen if isinstance(chosen, str) else chosen[0])
        destination.write_bytes(decode_file(file["base64"]))
        return {"saved": True, "name": destination.name}


class DesktopProjectActions:
    """Project routing, messaging, receivers, and local execution approval."""

    actions = frozenset(
        {
            "dashboard",
            "new-project",
            "join-project",
            "project",
            "invite",
            "message",
            "set-admin",
            "conversation-read",
            "memory-note-add",
            "memory-note-delete",
            "attachment-upload",
            "attachment-get",
            "receiver-attach",
            "receiver-start",
            "receiver-stop",
            "approve-execution",
        }
    )

    def __init__(self, bridge):
        self.bridge = bridge

    def invoke(self, action, data):
        bridge = self.bridge
        if not bridge.identity:
            raise PermissionError("请先登录")
        if action == "dashboard":
            result = bridge._dashboard()
            bridge.job_servers = {}
            for device in result.get("devices", []):
                device["local"] = device.get("node", device.get("id")) == bridge.profile["node"]
            for employee in result.get("employees", []):
                if not employee.get("pending"):
                    employee["pending"] = None
            result["pending_devices"] = list(bridge.inspected.values())
            result["pending_approvals"] = []
            for server in bridge._attached_services():
                runtime = bridge._runtime(server)
                for receipt in (runtime.root / "pull-receipts").glob("*.json"):
                    value = read_json(receipt, {})
                    if value.get("phase") == "waiting_local_approval":
                        job = value["job"]
                        backend = job["backend_project"]
                        template_name = job["request"].get("template")
                        template = read_json(
                            runtime.root / "projects" / backend / "execution_templates.json",
                            {},
                        ).get(template_name)
                        project = read_json(runtime.root / "projects" / backend / "project.json", {})
                        bridge.job_servers[job["id"]] = server
                        result["pending_approvals"].append(
                            {
                                "job_id": job["id"],
                                "template": template_name,
                                "expected_template": template,
                                "argv": (template or {}).get("argv"),
                                "cwd": project.get("remote_path"),
                                "node": bridge.profile["node"],
                                "server": server,
                            }
                        )
            return result
        if action == "new-project":
            server = data.get("server") or bridge.profile["server"]
            if server == "http://127.0.0.1:8765" and not bridge.profile.get("local_host_enabled"):
                bridge._enable_local_host()
            if server not in bridge._services():
                raise ValueError("请先加入该协作服务")
            owned = next(
                (item.get("hosted_by_me") for item in bridge._dashboard()["services"] if item["server"] == server),
                False,
            )
            if not owned:
                raise PermissionError("只能在自己账户名下的协作服务创建项目")
            bridge._activate_service(server)
            result = bridge._rpc("create-project", {"name": data["name"]}, server=server)
            bridge.project_servers[result["id"]] = server
            return dict(result, server=server)
        if action == "join-project":
            server = bridge._server(data.get("server") or bridge.profile["server"])
            if server in bridge._services():
                result = bridge._rpc("join-project", {"invite": data["invite"]}, server=server)
            else:
                challenge = bridge._post(
                    server,
                    {"action": "challenge", "public_key": bridge.identity["public_key"]},
                )
                payload = {
                    "invite": data["invite"],
                    "name": bridge.profile["username"],
                    "device_name": socket.gethostname(),
                }
                envelope = sign_envelope(
                    bridge.identity,
                    {
                        "domain": "harness-account-request-v1",
                        "nonce": challenge["nonce"],
                        "operation": "accept-project-invite",
                        "payload": payload,
                    },
                )
                result = bridge._post(
                    server,
                    {"action": "accept-project-invite", "envelope": envelope},
                )
                bridge.activated_services.add(server)
                bridge.profile.setdefault("services", []).append(server)
                bridge._save_public_profile()
                bridge._sync_service_links(force=True)
            if bridge._mcp_grant_path().is_file():
                bridge._sync_mcp_delegate([server])
            bridge.project_servers[result["project"]] = server
            return dict(result, server=server)
        if action in {
            "project",
            "invite",
            "message",
            "set-admin",
            "conversation-read",
            "memory-note-add",
            "memory-note-delete",
            "attachment-upload",
            "attachment-get",
        }:
            project = data.get("project", data.get("id"))
            server = bridge.project_servers.get(project, bridge.profile["server"])
            payload = dict(data)
            if action == "message":
                payload.setdefault("client_message_id", str(uuid.uuid4()))
            result = bridge._rpc(
                action,
                payload,
                server=server,
                timeout=45 if action.startswith("attachment-") else 15,
            )
            if action == "project":
                result["server"] = server
            if action == "invite":
                from agent_harness.team_service import TeamService

                entry = TeamService(bridge.root, server).status()
                result["server"] = entry.get("url") if entry["state"] == "ready" else server
            return result
        if action == "receiver-attach":
            server = bridge._server(data["server"])
            if server not in bridge._services():
                raise ValueError("请先接受这个服务的项目邀请")
            bridge._activate_service(server)
            bridge._rpc("dashboard", server=server)
            bridge._enroll(server)
            if server != bridge.profile["server"] and server not in bridge.profile.setdefault("attached_services", []):
                bridge.profile["attached_services"].append(server)
                bridge._save_public_profile()
            return {"attached": True, "server": server}
        if action == "receiver-start":
            started = []
            for server in bridge._attached_services():
                bridge._activate_service(server)
                bridge._enroll() if server == bridge.profile["server"] else bridge._enroll(server)
                started.append(server)
            return {"started": started}
        if action == "receiver-stop":
            for server in bridge._attached_services():
                bridge._runtime(server).stop_and_wait()
            return {"stopped": True}
        return bridge._runtime(bridge._execution_server(data["job_id"]))._control(
            "action", dict(action="approve-execution", **data)
        )
