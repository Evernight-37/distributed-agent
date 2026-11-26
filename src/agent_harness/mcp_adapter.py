"""Codex-facing MCP adapter for the local Foolship account.

The adapter is a stdio process.  It opens no listening port and holds only a
server-enforced, capability-scoped delegate credential created by the desktop
control plane.  Project services remain authoritative for membership,
conversation visibility, employee ownership, and task state.
"""

import argparse
import json
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from agent_harness.account_identity import sign_envelope
from agent_harness.common import read_json
from agent_harness.pull_worker import validate_server
from agent_harness.broker_transport import broker_opener
from agent_harness.hub_api import account_response
from agent_harness.mcp_server import run_stdio
from agent_harness.protocol import ensure_hub_compatible


APPROVAL_KINDS = ("permission", "attachment", "execution")


class FoolshipMcpBackend:
    def __init__(self, grant_path, network_path=None, client="codex"):
        self.grant_path = Path(grant_path).expanduser().resolve()
        # Codex and DeepSeek Harness share this adapter; the label only records
        # which client sent the message, never a different identity.
        self.client = client if client in {"codex", "dsh"} else "codex"
        self.grant = self._load_grant()
        network_path = (
            Path(network_path).expanduser().resolve()
            if network_path
            else self.grant_path.parent.parent / "broker-network.json"
        )
        self.network_path = network_path
        self.project_servers = {}
        self.project_names = {}
        self.last_dashboard = 0.0
        self.dashboard_cache = None

    def _opener(self):
        """Reload the local broker proxy so long-lived MCP sessions update safely."""
        network = read_json(self.network_path, {})
        proxy = network.get("proxy") if isinstance(network, dict) else None
        return broker_opener(proxy)

    def _load_grant(self):
        value = read_json(self.grant_path)
        required = {
            "version",
            "identity",
            "profile_id",
            "username",
            "servers",
            "scopes",
        }
        if not isinstance(value, dict) or value.get("version") != 1 or not required.issubset(value):
            raise PermissionError("Foolship Codex integration is not enabled for this account")
        identity = value.get("identity")
        if not isinstance(identity, dict) or not all(
            isinstance(identity.get(key), str) for key in ("private_key", "public_key", "account_id")
        ):
            raise PermissionError("Foolship Codex delegate credential is invalid")
        scopes = value.get("scopes")
        if not isinstance(scopes, list) or not {
            "collaboration.read",
            "collaboration.message",
            "collaboration.task",
        }.issubset(scopes):
            raise PermissionError("Foolship Codex delegate scopes are incomplete")
        servers = value.get("servers")
        if not isinstance(servers, list) or not servers:
            raise PermissionError("Foolship has no collaboration service")
        for server in servers:
            validate_server(server)
        return value

    def _post(self, server, data, timeout=12):
        validate_server(server)
        request = urllib.request.Request(
            server.rstrip("/") + "/api/account",
            data=json.dumps(data, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with self._opener().open(request, timeout=timeout) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                message = json.load(exc).get("error", "协作服务拒绝请求")
            except Exception:
                message = "协作服务拒绝请求"
            raise PermissionError(message) from None
        except urllib.error.URLError:
            raise RuntimeError("无法连接协作服务 " + server + "；请确认托管电脑和 Tailscale 在线") from None
        value = account_response(data, result)
        if data.get("action") == "challenge":
            ensure_hub_compatible(value)
        return value

    def _rpc(self, server, operation, payload=None, timeout=12):
        identity = self.grant["identity"]
        challenge = self._post(
            server,
            {"action": "challenge", "public_key": identity["public_key"]},
            timeout,
        )
        features = set(challenge.get("features", []))
        if "codex_mcp_v1" not in features:
            raise RuntimeError("协作服务版本过旧，尚未启用 Codex MCP 权限边界")
        if operation in {"mcp-approvals", "mcp-approval-decide"} and "mcp_project_approvals_v1" not in features:
            raise RuntimeError("协作服务尚未启用 MCP 项目审批")
        if operation == "project-work" and "project_work_v1" not in features:
            raise RuntimeError("协作服务尚未启用项目任务")
        if operation == "memory-note-review" and "project_memory_review_v1" not in features:
            raise RuntimeError("协作服务尚未启用项目记忆审核")
        if operation == "task" and payload and "source_message_id" in payload and "task_trace_v1" not in features:
            raise RuntimeError("协作服务尚不支持用原消息关联任务")
        envelope = sign_envelope(
            identity,
            {
                "domain": "harness-account-request-v1",
                "nonce": challenge["nonce"],
                "operation": operation,
                "payload": payload or {},
            },
        )
        return self._post(server, {"action": "rpc", "envelope": envelope}, timeout)

    def _services(self):
        services = list(dict.fromkeys(self.grant["servers"]))
        primary = services[0]
        discovery_failed = False
        try:
            linked = self._rpc(primary, "service-links", timeout=5).get("servers", [])
        except (OSError, RuntimeError, PermissionError, ValueError):
            linked = []
            discovery_failed = True
        for server in linked:
            try:
                validate_server(server)
            except (TypeError, ValueError):
                continue
            if server not in services:
                services.append(server)
        return services, discovery_failed

    def _dashboards(self, force=False):
        if not force and self.dashboard_cache is not None and time.monotonic() - self.last_dashboard < 3:
            return self.dashboard_cache
        available = []
        unavailable = []
        projects = {}
        names = {}
        service_ids = set()
        services, discovery_failed = self._services()
        for server in services:
            try:
                data = self._rpc(server, "dashboard", timeout=6)
            except (OSError, RuntimeError, PermissionError, ValueError) as exc:
                unavailable.append({"server": server, "error": str(exc)})
                continue
            service_id = data.get("service_id")
            if service_id and service_id in service_ids:
                continue
            if service_id:
                service_ids.add(service_id)
            available.append({"server": server, "data": data})
            for project in data.get("projects", []):
                projects[project["id"]] = server
                name = project.get("name")
                if name:
                    names.setdefault(name, set()).add(project["id"])
        self.project_servers = projects
        self.project_names = names
        self.dashboard_cache = {
            "available": available,
            "unavailable": unavailable,
            "discovery_failed": discovery_failed,
        }
        self.last_dashboard = time.monotonic()
        return self.dashboard_cache

    def _resolve(self, project):
        """A project id, or the human-typed name mapped onto one."""
        if project in self.project_servers:
            return project
        matches = self.project_names.get(project, ())
        if len(matches) > 1:
            raise ValueError("有多个同名项目，请使用项目编号")
        return next(iter(matches), project)

    def _project_route(self, project):
        for force in (False, True):
            dashboard = self._dashboards(force=force)
            project_id = self._resolve(project)
            server = self.project_servers.get(project_id)
            if server:
                return project_id, server
        if dashboard["unavailable"] or dashboard["discovery_failed"]:
            raise RuntimeError("有协作服务暂时无法查询，无法确认此项目是否可见")
        raise ValueError("当前账户看不到这个项目")

    def _approval_snapshot(self, project):
        if project is not None:
            if not isinstance(project, str) or not project.strip():
                raise ValueError("项目不能为空")
            project_id, server = self._project_route(project)
            return self._rpc(server, "mcp-approvals", {"project": project_id}, timeout=6)
        dashboard = self._dashboards()
        requests = []
        unavailable = list(dashboard["unavailable"])
        for service in dashboard["available"]:
            server = service["server"]
            try:
                requests.extend(self._rpc(server, "mcp-approvals", timeout=6)["requests"])
            except (OSError, RuntimeError, PermissionError, ValueError) as exc:
                unavailable.append({"server": server, "error": str(exc)})
        return {
            "requests": sorted(requests, key=lambda request: (request["created_at"], request["id"])),
            "unavailable_services": unavailable,
            "service_discovery_incomplete": dashboard["discovery_failed"],
        }

    def _approvals_list(self, arguments):
        wait = arguments.get("wait_seconds", 0)
        if type(wait) is not int or not 0 <= wait <= 20:
            raise ValueError("wait_seconds 必须是 0 到 20 的整数")
        seen = arguments.get("seen_ids", [])
        if (
            not isinstance(seen, list)
            or len(seen) > 1000
            or any(not isinstance(item, str) or not 1 <= len(item) <= 120 for item in seen)
        ):
            raise ValueError("seen_ids 必须是最多 1000 个申请编号的列表")
        seen = set(seen)
        deadline = time.monotonic() + wait
        while True:
            snapshot = self._approval_snapshot(arguments.get("project"))
            remaining = deadline - time.monotonic()
            if remaining <= 0 or any(request["id"] not in seen for request in snapshot["requests"]):
                return snapshot
            time.sleep(min(2, remaining))

    def tool_specs(self):
        obj = {"type": "object", "additionalProperties": False}
        return [
            {
                "name": "harness_status",
                "description": "检查 Foolship 账户及各协作服务连接状态。",
                "inputSchema": dict(obj, properties={}),
            },
            {
                "name": "harness_projects_list",
                "description": "列出当前 Foolship 账户参加的项目及其托管服务状态。",
                "inputSchema": dict(obj, properties={}),
            },
            {
                "name": "harness_project_read",
                "description": "读取一个项目中当前账户有权查看的成员、会话、消息、任务与共享记忆。可按会话和消息游标缩小结果。",
                "inputSchema": dict(
                    obj,
                    properties={
                        "project": {"type": "string"},
                        "conversation_id": {"type": "string"},
                        "after_message_id": {"type": "integer", "minimum": 0},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                    },
                    required=["project"],
                ),
            },
            {
                "name": "harness_project_memory_review",
                "description": (
                    "按编号确认、归档或合并项目记忆。用 harness_project_read 查看项目记忆候选。"
                    "需要当前账户在该项目启用 MCP 项目记忆审核权限。"
                ),
                "inputSchema": dict(
                    obj,
                    properties={
                        "project": {"type": "string"},
                        "ids": {
                            "type": "array",
                            "items": {"type": "integer", "minimum": 1},
                            "minItems": 1,
                            "maxItems": 30,
                        },
                        "action": {"type": "string", "enum": ["keep", "archive", "merge"]},
                        "body": {"type": "string", "minLength": 1, "maxLength": 2000},
                        "kind": {"type": "string", "enum": ["rule", "decision", "fact"]},
                    },
                    required=["project", "ids", "action"],
                ),
            },
            {
                "name": "harness_message_send",
                "description": (
                    "代表账户本人向项目群聊、自己的 Agent 或另一位人类发送消息。"
                    "新版服务中，群聊使用 mention_agent_id 点名才会唤醒 Agent，交给管理员时填管理员的 Agent 编号。"
                    "返回的 conversation_id 与 id 可用于读取回复。"
                ),
                "inputSchema": dict(
                    obj,
                    properties={
                        "project": {"type": "string"},
                        "body": {"type": "string", "minLength": 1, "maxLength": 16000},
                        "conversation_id": {"type": "string"},
                        "recipient_id": {"type": "string"},
                        "mention_agent_id": {"type": "string"},
                        "client_message_id": {"type": "string"},
                    },
                    required=["project", "body"],
                ),
            },
            {
                "name": "harness_task_assign",
                "description": "给当前账户拥有且已派入项目的 Agent 创建异步任务。立即返回任务编号；不要在本次调用中等待模型完成。audience 决定结果去哪：group（默认）发到项目群聊；private 只发到你和该 Agent 的私信，需要非公开结果时用它。两种结果都可以用 harness_project_read 读回。",
                "inputSchema": dict(
                    obj,
                    properties={
                        "project": {"type": "string"},
                        "agent": {"type": "string"},
                        "title": {"type": "string"},
                        "instructions": {"type": "string"},
                        "stage": {
                            "type": "string",
                            "enum": ["discuss", "review", "implement"],
                        },
                        "audience": {
                            "type": "string",
                            "enum": ["group", "private"],
                            "description": "group 把结果发到项目群聊（默认）；private 只发到你和该 Agent 的私信。",
                        },
                        "allowed_files": {
                            "type": "string",
                            "description": "声明的修改范围；这是任务合同，不是文件系统 ACL。实现任务仍可写机器所有者登记的整个项目工作区。",
                        },
                        "acceptance": {"type": "string"},
                        "validation": {"type": "string"},
                        "source_message_id": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "可选，关联本项目内你能看到的原消息编号。",
                        },
                    },
                    required=[
                        "project",
                        "agent",
                        "title",
                        "instructions",
                        "acceptance",
                        "validation",
                    ],
                ),
            },
            {
                "name": "harness_project_work",
                "description": (
                    "管理可继续执行的项目任务；用 harness_project_read 获取 work 下的草稿和任务编号。"
                    "create 直接建任务；draft-discuss 把补充意见定向给管理员；revise 修改草稿；"
                    "confirm 确认用户看过的指定草稿版本。follow-up 补充执行要求、reference 关联参考消息、"
                    "accept 验收交付、reopen 重新打开已验收的任务、update-scope 基于当前版本修改任务范围。"
                    "每个操作生成 UUID，重试沿用同一 UUID。"
                ),
                "inputSchema": dict(
                    obj,
                    properties={
                        "project": {"type": "string"},
                        "action": {
                            "type": "string",
                            "enum": [
                                "create",
                                "revise",
                                "confirm",
                                "draft-discuss",
                                "follow-up",
                                "reference",
                                "accept",
                                "reopen",
                                "update-scope",
                            ],
                        },
                        "client_request_id": {"type": "string", "format": "uuid"},
                        "draft_id": {"type": "string"},
                        "revision": {"type": "integer", "minimum": 1},
                        "work_id": {"type": "string"},
                        "body": {"type": "string"},
                        "message_id": {"type": "integer", "minimum": 1},
                        "source_message_id": {"type": "integer", "minimum": 1},
                        "reference_message_ids": {
                            "type": "array",
                            "maxItems": 30,
                            "items": {"type": "integer", "minimum": 1},
                        },
                        "conversation_id": {"type": "string"},
                        "visibility": {"type": "string", "enum": ["group", "private"]},
                        "agent": {"type": "string"},
                        "title": {"type": "string"},
                        "instructions": {"type": "string"},
                        "deliverable": {"type": "string"},
                        "acceptance": {"type": "string"},
                        "validation": {"type": "string"},
                        "allowed_files": {"type": "string"},
                        "stage": {"type": "string", "enum": ["discuss", "implement"]},
                    },
                    required=["project", "action", "client_request_id"],
                ),
            },
            {
                "name": "harness_task_get",
                "description": "读取异步任务的当前状态、结果或错误，以及有权查看的消息来源、负责人、执行设备和交付会话。",
                "inputSchema": dict(
                    obj,
                    properties={
                        "project": {"type": "string"},
                        "task": {"type": "string"},
                    },
                    required=["project", "task"],
                ),
            },
            {
                "name": "harness_task_cancel",
                "description": "请求取消此 MCP 委托身份自己创建的任务。已经交给设备的任务须等待接收端确认。",
                "inputSchema": dict(
                    obj,
                    properties={
                        "project": {"type": "string"},
                        "task": {"type": "string"},
                    },
                    required=["project", "task"],
                ),
            },
            {
                "name": "harness_approvals_list",
                "description": (
                    "收取当前人类账户名下机器的待审批通知，包括额外执行权限、附件发送和已登记命令。"
                    "可以告知用户；can_decide 为真时可按该项目授予的权限代为处理。"
                    "可传 seen_ids 和 wait_seconds 等待新申请。"
                ),
                "inputSchema": dict(
                    obj,
                    properties={
                        "project": {"type": "string", "description": "可选项目名称或编号，省略时查所有已连接项目"},
                        "wait_seconds": {"type": "integer", "minimum": 0, "maximum": 20},
                        "seen_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 1000},
                    },
                ),
            },
            {
                "name": "harness_approval_decide",
                "description": "按设备所有者在项目中授予的委托权限批准或拒绝一条申请。附件或命令必须带通知里的 digest。",
                "inputSchema": dict(
                    obj,
                    properties={
                        "project": {"type": "string"},
                        "kind": {"type": "string", "enum": list(APPROVAL_KINDS)},
                        "id": {"type": "string"},
                        "approve": {"type": "boolean"},
                        "digest": {"type": "string"},
                    },
                    required=["project", "kind", "id", "approve"],
                ),
            },
        ]

    def call(self, name, arguments):
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be an object")
        if name == "harness_status":
            data = self._dashboards(force=True)
            return {
                "account": self.grant["username"],
                "profile_id": self.grant["profile_id"],
                "connected_services": [item["server"] for item in data["available"]],
                "unavailable_services": data["unavailable"],
            }
        if name == "harness_projects_list":
            data = self._dashboards(force=True)
            projects = []
            for item in data["available"]:
                for project in item["data"].get("projects", []):
                    projects.append(
                        {
                            key: project.get(key)
                            for key in (
                                "id",
                                "name",
                                "owner_id",
                                "admin_agent_id",
                                "paused",
                                "archived_at",
                                "last_entered",
                            )
                        }
                        | {"server": item["server"]}
                    )
            return {"projects": projects, "unavailable_services": data["unavailable"]}
        if name == "harness_project_read":
            project, server = self._project_route(arguments["project"])
            result = self._rpc(server, "project", {"project": project})
            messages = result.get("messages", [])
            conversation = arguments.get("conversation_id")
            if conversation:
                messages = [message for message in messages if message.get("conversation_id") == conversation]
            after = arguments.get("after_message_id", 0)
            if not isinstance(after, int) or isinstance(after, bool) or after < 0:
                raise ValueError("after_message_id 必须是非负整数")
            messages = [message for message in messages if int(message.get("id", 0)) > after]
            limit = arguments.get("limit", 100)
            if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
                raise ValueError("limit 必须是 1 到 200")
            result["messages"] = messages[-limit:]
            result["server"] = server
            return result
        if name == "harness_message_send":
            payload = {
                key: arguments[key]
                for key in (
                    "body",
                    "conversation_id",
                    "recipient_id",
                    "mention_agent_id",
                )
                if key in arguments
            }
            message_id = arguments.get("client_message_id") or str(uuid.uuid4())
            try:
                message_id = str(uuid.UUID(message_id))
            except (ValueError, TypeError, AttributeError):
                raise ValueError("client_message_id 必须是 UUID") from None
            payload["client_message_id"] = message_id
            payload["client"] = self.client
            project, server = self._project_route(arguments["project"])
            payload["project"] = project
            result = self._rpc(server, "message", payload)
            return {"project": project, "client_message_id": message_id, **result}
        if name == "harness_task_assign":
            project, server = self._project_route(arguments["project"])
            payload = dict(arguments, project=project)
            payload.setdefault("stage", "discuss")
            payload.setdefault("allowed_files", "只讨论，不修改文件")
            payload["client"] = self.client
            result = self._rpc(server, "task", payload)
            return {"project": project, **result}
        if name == "harness_task_get":
            project, server = self._project_route(arguments["project"])
            task = self._rpc(server, "task-status", {"project": project, "task": arguments["task"]})
            if task.get("project_id") != project:
                raise ValueError("任务不在指定项目中")
            return task
        if name == "harness_project_work":
            project, server = self._project_route(arguments["project"])
            result = self._rpc(server, "project-work", dict(arguments, project=project))
            return {"project": project, **result}
        if name == "harness_project_memory_review":
            project, server = self._project_route(arguments["project"])
            payload = {key: arguments[key] for key in ("ids", "action", "body", "kind") if key in arguments}
            return self._rpc(server, "memory-note-review", {**payload, "project": project})
        if name == "harness_task_cancel":
            project, server = self._project_route(arguments["project"])
            return self._rpc(server, "task-cancel", {"project": project, "task_id": arguments["task"]})
        if name == "harness_approvals_list":
            return self._approvals_list(arguments)
        if name == "harness_approval_decide":
            project, server = self._project_route(arguments["project"])
            payload = {key: arguments[key] for key in ("kind", "id", "approve", "digest") if key in arguments}
            payload.update(project=project, client=self.client)
            return self._rpc(server, "mcp-approval-decide", payload)
        raise ValueError("Unknown Foolship tool")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--grant", required=True)
    parser.add_argument("--network")
    parser.add_argument("--client", default="codex", choices=("codex", "dsh"))
    args = parser.parse_args(argv)
    run_stdio(FoolshipMcpBackend(args.grant, args.network, args.client))


if __name__ == "__main__":
    main()
