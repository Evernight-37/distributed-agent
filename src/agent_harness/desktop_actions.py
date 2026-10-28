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
            "employee-delete-preview",
            "employee-configure",
            "employee-dispatch",
            "employee-withdraw",
            "workspace-configure",
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
        if action == "employee-delete-preview":
            employee = bridge._employee(data["id"])
            return bridge._rpc("employee-delete-preview", {"id": employee["id"]}, server=employee["server"], timeout=20)
        if action == "employee-delete":
            employee = bridge._employee(data["id"])
            return bridge._owner("delete", employee["node"], {"id": employee["id"]}, employee["server"])
        if action == "employee-configure":
            employee = bridge._employee(data["id"])
            settings = {k: data[k] for k in ("model", "effort", "enabled", "stages", "timeout")}
            settings["strict_worktree"] = bool(data.get("strict_worktree", False))
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
        if action == "workspace-configure":
            server = bridge.project_servers.get(data["project"], data.get("server") or bridge.profile["server"])
            if data["node"] != bridge.profile["node"]:
                command = bridge._owner(
                    "workspace-default",
                    data["node"],
                    {"project_id": data["project"]},
                    server=server,
                )
                return {"configured": False, "pending": True, "command": command}
            if not bridge.window:
                raise RuntimeError("文件夹选择器尚未就绪")
            import webview
            from agent_harness.owner_worker import OwnerControl
            from agent_harness.pull_worker import Connector

            chosen = bridge.window.create_file_dialog(webview.FileDialog.FOLDER)
            if not chosen:
                return {"configured": False}
            path = chosen[0] if isinstance(chosen, (list, tuple)) else chosen
            envelope = sign_envelope(
                bridge.identity,
                {
                    "domain": "harness-owner-command-v1",
                    "account_id": bridge.identity["account_id"],
                    "target": bridge.profile["node"],
                    "id": "local-workspace-" + secrets.token_hex(16),
                    "action": "workspace-configure",
                    "data": {"project_id": data["project"], "path": path},
                },
            )
            result = OwnerControl(Connector(bridge._runtime(server).config_path)).apply(envelope)
            if result["state"] != "applied":
                raise RuntimeError(result.get("result", {}).get("error", "项目工作目录配置失败"))
            status = bridge._rpc("workspace-ready", {"project": data["project"]}, server=server, timeout=20)
            return {"configured": True, "status": status}
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


class DesktopTransferActions:
    """Account-level machine-to-machine file transfer controls."""

    actions = frozenset(
        {
            "file-transfer-dashboard",
            "file-transfer-send-select",
            "file-transfer-accept",
            "file-transfer-reject",
            "file-transfer-cancel",
            "file-transfer-receive-folder",
            "file-transfer-job-status",
        }
    )

    def __init__(self, bridge):
        self.bridge = bridge

    def _dashboard(self):
        bridge = self.bridge
        transfers, contacts, devices, unavailable = [], [], [], []
        services = [item["server"] for item in bridge._dashboard().get("services", [])]
        for server in services:
            try:
                peers = bridge._rpc("transfer-peers", server=server, timeout=10)
                listing = bridge._rpc("transfer-list", server=server, timeout=10)
            except (OSError, RuntimeError, ValueError, PermissionError):
                unavailable.append(server)
                continue
            for item in listing.get("transfers", []):
                transfers.append(dict(item, server=server))
            for item in peers.get("contacts", []):
                contacts.append(dict(item, server=server))
            for item in peers.get("devices", []):
                devices.append(dict(item, server=server))
        contact_names = {(item["server"], item["id"]): item["username"] for item in contacts}
        device_names = {(item["server"], item["node"]): item["name"] for item in devices}
        account_id = bridge.identity["account_id"]
        for item in transfers:
            if item["sender_id"] == item["recipient_id"] == account_id:
                peer_node = (
                    item.get("recipient_node")
                    if item.get("sender_node") == bridge.profile["node"]
                    else item.get("sender_node")
                )
                item["peer_name"] = device_names.get((item["server"], peer_node), "我的另一台机器")
            else:
                peer_id = item["recipient_id"] if item["sender_id"] == account_id else item["sender_id"]
                item["peer_name"] = contact_names.get((item["server"], peer_id), "协作成员")
        bridge.file_transfers.reconcile(transfers)
        return {
            "transfers": transfers,
            "contacts": contacts,
            "devices": devices,
            "jobs": bridge.file_transfers.jobs(),
            "settings": bridge.file_transfers.settings(),
            "unavailable_services": unavailable,
        }

    def invoke(self, action, data):
        bridge = self.bridge
        if not bridge.identity:
            raise PermissionError("请先登录")
        if action == "file-transfer-dashboard":
            return self._dashboard()
        if action == "file-transfer-job-status":
            return bridge.file_transfers.status(data["id"])
        if action == "file-transfer-receive-folder":
            node = data.get("node") or bridge.profile["node"]
            server = data.get("server") or bridge.profile["server"]
            if node != bridge.profile["node"]:
                remote_path = data.get("path")
                if not isinstance(remote_path, str) or not remote_path.strip():
                    raise ValueError("请填写目标机器上的绝对接收目录")
                command = bridge._owner(
                    "transfer-receive-directory",
                    node,
                    {"path": remote_path.strip()},
                    server=server,
                )
                return {"configured": False, "pending": True, "command": command}
            if not bridge.window:
                raise RuntimeError("文件夹选择器尚未就绪")
            import webview

            chosen = bridge.window.create_file_dialog(webview.FileDialog.FOLDER)
            if not chosen:
                return {"configured": False}
            folder = chosen[0] if isinstance(chosen, (list, tuple)) else chosen
            return {"configured": True, **bridge.file_transfers.set_receive_folder(folder)}
        if action == "file-transfer-send-select":
            if not bridge.window:
                raise RuntimeError("文件选择器尚未就绪")
            import webview

            server = data["server"]
            if server not in bridge._services():
                raise ValueError("请选择当前账户已接入的协作服务")
            chosen = bridge.window.create_file_dialog(webview.FileDialog.OPEN)
            if not chosen:
                return {"selected": False}
            source = Path(chosen[0] if isinstance(chosen, (list, tuple)) else chosen)
            return {
                "selected": True,
                "job": bridge.file_transfers.send(
                    server,
                    data["recipient_id"],
                    data.get("recipient_node"),
                    source,
                ),
            }
        if action == "file-transfer-accept":
            server = data["server"]
            transfer = bridge._rpc("transfer-status", {"id": data["id"]}, server=server, timeout=20)
            recipient_node = data.get("recipient_node") or bridge.profile["node"]
            peers = bridge._rpc("transfer-peers", server=server, timeout=20)
            owned = {item["node"] for item in peers.get("devices", [])}
            if recipient_node not in owned:
                raise PermissionError("接收机器不属于当前账户")
            if recipient_node != bridge.profile["node"]:
                accepted = bridge._rpc(
                    "transfer-decide",
                    {"id": data["id"], "accept": True, "recipient_node": recipient_node},
                    server=server,
                    timeout=20,
                )
                return {"accepted": True, "remote": True, "transfer": accepted}
            if not bridge.window:
                raise RuntimeError("文件保存窗口尚未就绪")
            import webview
            from agent_harness.attachments import safe_name

            chosen = bridge.window.create_file_dialog(
                webview.FileDialog.SAVE,
                save_filename=safe_name(transfer["name"]),
            )
            if not chosen:
                return {"accepted": False}
            destination = Path(chosen[0] if isinstance(chosen, (list, tuple)) else chosen)
            return {
                "accepted": True,
                "job": bridge.file_transfers.accept(server, transfer, destination),
            }
        if action == "file-transfer-reject":
            return bridge._rpc(
                "transfer-decide", {"id": data["id"], "accept": False}, server=data["server"], timeout=20
            )
        if action == "file-transfer-cancel":
            return bridge._rpc("transfer-cancel", {"id": data["id"]}, server=data["server"], timeout=20)
        raise ValueError("未知文件传输操作")


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
