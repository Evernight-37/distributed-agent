"""Apply authenticated owner controls locally, without a shell or credential access."""

import hashlib
import os
import re
from pathlib import Path
from agent_harness.account_identity import verify_owner_command
from agent_harness.common import (
    LEGACY_REASONING_LEVELS,
    REASONING_LEVELS,
    identifier,
    lock,
    read_json,
    validate_project,
    write_json,
)
from agent_harness.owner_backend import PROJECT_AGENT_ACTIONS, canonical, execution_project, project_agent_target

END_STATES = {
    "needs_review",
    "completed",
    "failed",
    "cancelled",
    "timed_out",
    "interrupted",
}


def key(value):
    return hashlib.sha256(value.encode()).hexdigest()


class OwnerControl:
    def __init__(self, connector):
        self.connector = connector
        self.root = Path(connector.root)

    def apply(self, envelope):
        # Invalid identities never create receipts or disclose directory contents.
        verify_owner_command(envelope, expected_account=self.connector.config["owner_account_id"])
        body = envelope["body"]
        if body.get("domain") != "harness-owner-command-v1" or body.get("target") != self.connector.config["node"]:
            raise PermissionError("Wrong command target")
        cid = body.get("id")
        if not isinstance(cid, str) or not 1 <= len(cid) <= 120:
            raise ValueError("Invalid command id")
        fingerprint = key(canonical(body))
        receipt = self.root / "owner-receipts" / (key(cid) + ".json")
        with lock(self.root / "owner-control.lock"):
            old = read_json(receipt)
            if old:
                if old["fingerprint"] != fingerprint:
                    raise ValueError("Command id already used")
                if old["phase"] == "done":
                    return old["response"]
                response = {
                    "state": "failed",
                    "result": {
                        "error": "Interrupted owner action requires local reconciliation; it will not be repeated"
                    },
                }
                write_json(
                    receipt,
                    {"fingerprint": fingerprint, "phase": "done", "response": response},
                )
                return response
            write_json(receipt, {"fingerprint": fingerprint, "phase": "started"})
            try:
                result = self._run(body["action"], self._payload(body))
                response = {"state": "applied", "result": result}
            except Exception as exc:
                response = {"state": "failed", "result": {"error": str(exc)}}
            write_json(
                receipt,
                {"fingerprint": fingerprint, "phase": "done", "response": response},
            )
            return response

    def _payload(self, body):
        """The command payload, opening a sealed one for this machine only.

        The Hub routes and stores these envelopes, so a payload it must not read
        (a local proxy setting, for instance) arrives sealed to this device's key.
        Routing fields stay in the clear for the Hub to authorize; the sealed part
        holds everything else and is merged over them. Older senders still write
        the whole payload in the clear; that path stays readable.
        """
        cleartext = dict(body["data"]) if isinstance(body.get("data"), dict) else {}
        sealed = body.get("sealed")
        if sealed is None:
            return cleartext
        if body["action"] in PROJECT_AGENT_ACTIONS:
            raise PermissionError("项目 Agent 操作必须使用可核对的项目标识")
        from agent_harness import sealing

        pair = self.connector.config.get("encrypt_key")
        if not isinstance(pair, dict) or not pair.get("private_key"):
            raise PermissionError("这台机器还没有加密密钥；请更新应用后重新配对")
        opened = sealing.open_sealed(
            pair["private_key"],
            sealed,
            {"target": body.get("target"), "id": body.get("id")},
        )
        cleartext.update(opened)
        return cleartext

    def _employees(self):
        return read_json(self.root / "owner-employees.json", {"employees": {}})["employees"]

    def _save(self, employees):
        write_json(self.root / "owner-employees.json", {"employees": employees})

    def _workspace_file(self):
        return self.root / "project-workspaces.json"

    def _workspaces(self):
        """Return local project-to-directory mappings, migrating legacy bindings.

        Absolute paths are deliberately kept in this receiver-local file.  A
        binding/advertisement contains only the shared project id and therefore
        cannot disclose a machine owner's filesystem layout to the Hub.
        """

        document = read_json(self._workspace_file(), {"version": 1, "workspaces": {}})
        if not isinstance(document, dict) or not isinstance(document.get("workspaces"), dict):
            raise ValueError("Invalid local project workspace registry")
        changed = document.get("version") != 1
        document["version"] = 1
        workspaces = document["workspaces"]
        by_project = {}
        for binding in self.connector.bindings():
            if binding.get("agent_workspace"):
                continue
            project_id = binding.get("project_id")
            backend = binding.get("backend_project")
            if not isinstance(project_id, str) or not isinstance(backend, str):
                continue
            project = read_json(self.root / "projects" / backend / "project.json", {})
            remote = project.get("remote_path") if isinstance(project, dict) else None
            if not isinstance(remote, str):
                continue
            try:
                path = Path(remote).resolve()
            except OSError:
                continue
            by_project.setdefault(project_id, set()).add(str(path))
        for project_id, candidates in by_project.items():
            if project_id in workspaces:
                continue
            ordered = sorted(candidates, key=str.casefold)
            if len(ordered) == 1:
                workspaces[project_id] = {"status": "ready", "remote_path": ordered[0]}
            else:
                workspaces[project_id] = {"status": "needs_choice", "candidates": ordered}
            changed = True
        if changed:
            write_json(self._workspace_file(), document)
        return workspaces

    def _workspace(self, project_id):
        workspace = self._workspaces().get(project_id)
        if not workspace:
            raise ValueError("请先为这个项目在本机选择工作目录")
        if workspace.get("status") != "ready":
            raise ValueError("旧版员工为这个项目使用了不同目录，请先选择项目工作目录")
        path = Path(workspace.get("remote_path", ""))
        if not path.is_absolute() or path == Path(path.anchor) or not path.is_dir():
            raise ValueError("项目工作目录不存在，请重新选择")
        return str(path.resolve())

    def _configure_workspace(self, data):
        project_id = data.get("project_id")
        if not isinstance(project_id, str) or not 1 <= len(project_id) <= 120:
            raise ValueError("Invalid shared project")
        remote = data.get("path")
        if not isinstance(remote, str):
            raise ValueError("Choose an existing absolute directory")
        path = Path(remote)
        if not path.is_absolute() or path == Path(path.anchor) or not path.is_dir():
            raise ValueError("Choose an existing absolute directory, not a drive root")
        resolved = path.resolve()
        if resolved == Path(resolved.anchor):
            raise ValueError("Choose an existing absolute directory, not a drive root")
        remote = str(resolved)
        bindings = [
            b for b in self.connector.bindings() if b.get("project_id") == project_id and not b.get("agent_workspace")
        ]
        # Preserve each backend project's independent Codex sessions while all
        # employees dispatched to this shared project use the same local tree.
        with lock(self.root / "registry.lock"):
            for binding in bindings:
                self._idle(binding["backend_project"])
            document = read_json(self._workspace_file(), {"version": 1, "workspaces": {}})
            if not isinstance(document, dict) or not isinstance(document.get("workspaces"), dict):
                raise ValueError("Invalid local project workspace registry")
            document["version"] = 1
            document["workspaces"][project_id] = {"status": "ready", "remote_path": remote}
            for binding in bindings:
                project_path = self.root / "projects" / binding["backend_project"] / "project.json"
                project = read_json(project_path)
                if project:
                    project["remote_path"] = remote
                    validate_project(project)
                    write_json(project_path, project)
            write_json(self._workspace_file(), document)
        return {"project_id": project_id, "configured": True}

    def _configure_default_workspace(self, data):
        """Create a receiver-local default without disclosing its path to the Hub."""

        project_id = data.get("project_id")
        if not isinstance(project_id, str) or not 1 <= len(project_id) <= 120:
            raise ValueError("Invalid shared project")
        receiver_root = self.root.resolve()
        workspace_root = receiver_root / "workspaces"
        workspace_root.mkdir(parents=True, exist_ok=True)
        resolved_root = workspace_root.resolve()
        if not resolved_root.is_relative_to(receiver_root):
            raise ValueError("Default workspace root must remain inside the receiver data directory")
        target = (resolved_root / ("project-" + key(project_id)[:32])).resolve()
        if not target.is_relative_to(resolved_root):
            raise ValueError("Invalid default workspace")

        bindings = [
            b for b in self.connector.bindings() if b.get("project_id") == project_id and not b.get("agent_workspace")
        ]
        with lock(self.root / "registry.lock"):
            document = read_json(self._workspace_file(), {"version": 1, "workspaces": {}})
            if not isinstance(document, dict) or not isinstance(document.get("workspaces"), dict):
                raise ValueError("Invalid local project workspace registry")
            existing = document["workspaces"].get(project_id)
            if isinstance(existing, dict) and existing.get("status") == "ready":
                current = Path(existing.get("remote_path", ""))
                if current.is_absolute() and current.is_dir():
                    return {"project_id": project_id, "configured": True, "existing": True}
            for binding in bindings:
                self._idle(binding["backend_project"])
            target.mkdir(parents=True, exist_ok=True)
            document["version"] = 1
            document["workspaces"][project_id] = {
                "status": "ready",
                "remote_path": str(target),
            }
            for binding in bindings:
                project_path = self.root / "projects" / binding["backend_project"] / "project.json"
                project = read_json(project_path)
                if project:
                    project["remote_path"] = str(target)
                    validate_project(project)
                    write_json(project_path, project)
            write_json(self._workspace_file(), document)
        return {"project_id": project_id, "configured": True, "default": True}

    def _configure_receive_directory(self, data):
        """Persist an owner-selected receive directory only on this receiver."""

        remote = data.get("path")
        if not isinstance(remote, str) or not 1 <= len(remote) <= 4096 or "\0" in remote:
            raise ValueError("Choose an absolute directory")
        path = Path(remote)
        if not path.is_absolute() or path.is_symlink():
            raise ValueError("Choose an absolute directory, not a symbolic link")
        # A receiver usually receives into a folder it creates for the user: an
        # owner naming a new directory should not have to create it by hand first.
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
        if not path.is_dir():
            raise ValueError("The chosen path is not a directory")
        resolved = path.resolve()
        if resolved == Path(resolved.anchor):
            raise ValueError("Choose a directory, not a filesystem root")
        settings = self.root / "file-transfer-settings.json"
        write_json(
            settings,
            {"version": 1, "receive_folder": str(resolved)},
        )
        try:
            os.chmod(settings, 0o600)
        except OSError:
            # Windows receiver data ACLs are applied by the desktop bootstrap.
            pass
        return {"configured": True}

    def _remember_network(self, backend, network):
        """Persist the agent's network policy next to the local project record.

        The worker reads ``project.json`` for every turn, so the policy travels
        with the local registration instead of being re-sent per job.
        """
        if not isinstance(network, dict):
            return
        path = self.root / "projects" / backend / "project.json"
        record = read_json(path, {})
        if not isinstance(record, dict) or not record:
            return
        if record.get("network") != network:
            record["network"] = network
            write_json(path, record)

    def _bindings(self, employee):
        return [
            b for b in self.connector.bindings() if b.get("employee_id") == employee and not b.get("project_scoped")
        ]

    def _idle(self, backend):
        if read_json(self.root / "projects" / backend / "active.json"):
            raise ValueError("Employee has an active job; retry after it finishes")
        for area in ("jobs", "executions"):
            for path in (self.root / area).glob("*/state.json"):
                value = read_json(path, {})
                if value.get("project") == backend and value.get("status") not in END_STATES:
                    raise ValueError("Employee has an unresolved job")

    def _settings(self, settings):
        # remote_path is accepted only so an installed pre-v2 employee record
        # can be read and rewritten.  It is never returned or used for a new
        # dispatch; directories now belong to project x machine.
        allowed = {
            "remote_path",
            "model",
            "effort",
            "timeout",
            "stages",
            "enabled",
            "strict_worktree",
            "attachment_policy",
            "network",
            "model_config_version",
        }
        if not isinstance(settings, dict) or set(settings) - allowed:
            raise ValueError("Unsupported employee setting")
        model = settings.get("model")
        if not isinstance(model, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]+", model):
            raise ValueError("An explicit model is required")
        effort = settings.get("effort")
        model_version = settings.get("model_config_version")
        if model_version is not None and model_version != 2:
            raise ValueError("Unsupported model configuration version")
        allowed_levels = REASONING_LEVELS if model_version == 2 else LEGACY_REASONING_LEVELS
        if not isinstance(effort, str) or effort not in allowed_levels:
            raise ValueError("Invalid reasoning effort")
        timeout = settings.get("timeout", 1800)
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not 30 <= timeout <= 86400:
            raise ValueError("Timeout must be between 30 and 86400 seconds")
        stages = settings.get("stages", ["discuss"])
        if not isinstance(stages, list) or not stages or set(stages) - {"discuss", "review", "implement"}:
            raise ValueError("Invalid employee stages")
        if not isinstance(settings.get("enabled", False), bool):
            raise ValueError("enabled must be boolean")
        if not isinstance(settings.get("strict_worktree", False), bool):
            raise ValueError("strict_worktree must be boolean")
        attachment_policy = settings.get("attachment_policy", "ask")
        if not isinstance(attachment_policy, str) or attachment_policy not in {"ask", "auto", "full"}:
            raise ValueError("Invalid attachment policy")
        network = settings.get("network", {"mode": "machine"})
        if not isinstance(network, dict) or set(network) - {"mode", "port"}:
            raise ValueError("Invalid network policy")
        mode = network.get("mode", "machine")
        if mode not in {"machine", "direct", "custom"}:
            raise ValueError("Invalid network mode")
        if mode == "custom":
            port = network.get("port")
            if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                raise ValueError("Proxy port must be between 1 and 65535")
            network = {"mode": "custom", "port": port}
        else:
            network = {"mode": mode}
        return {
            "model": model,
            "effort": effort,
            "timeout": timeout,
            "stages": stages,
            "enabled": settings.get("enabled", False),
            "strict_worktree": settings.get("strict_worktree", False),
            "attachment_policy": attachment_policy,
            "network": network,
            **({"model_config_version": model_version} if model_version is not None else {}),
        }

    def _write_project_settings(self, backend, settings, workspace=None):
        path = self.root / "projects" / backend / "project.json"
        options = {k: settings[k] for k in ("model", "effort", "timeout", "strict_worktree")}
        if "model_config_version" in settings:
            options["model_config_version"] = settings["model_config_version"]
        registered = read_json(path)
        if registered is None:
            if workspace is None:
                raise ValueError("找不到项目 Agent 的本机配置")
            self.connector.executor(
                {
                    "action": "register",
                    "project": dict(name=backend, remote_path=workspace, **options),
                    "memory": {"revision": 0, "facts": [], "decisions": [], "open_questions": []},
                }
            )
        else:
            if workspace is not None and Path(registered["remote_path"]).resolve() != Path(workspace).resolve():
                raise ValueError("Existing project directory differs; choose the project workspace again")
            self.connector.executor({"action": "configure", "project": backend, **options})
        self._remember_network(backend, settings["network"])

    def _agent_workspace(self, data, backend):
        if data["workspace_mode"] == "dedicated":
            parent = self.root.resolve() / "agent-workspaces"
            target = parent / backend
            if target.resolve() != target:
                raise ValueError("Agent 工作目录不能重定向到其他位置")
        else:
            from agent_harness import sealing

            pair = self.connector.config.get("encrypt_key") or {}
            if not pair.get("private_key"):
                raise ValueError("执行机器缺少目录解密密钥")
            value = sealing.open_sealed(
                pair["private_key"],
                data["sealed_workspace"],
                {"target": self.connector.config["node"], "id": data["id"], "project": data["project_id"]},
            ).get("path")
            if not isinstance(value, str) or not value or len(value) > 4096 or "\0" in value:
                raise ValueError("请输入执行机器上已有目录的绝对路径")
            target = Path(value)
            if not target.is_absolute() or not target.is_dir():
                raise ValueError("执行机器上找不到指定的绝对目录")
            target = target.resolve()
            if target == Path(target.anchor):
                raise ValueError("请选择根目录下的项目目录")
        for record in (self.root / "projects").glob("*/project.json"):
            if record.parent.name == backend:
                continue
            other = read_json(record).get("remote_path")
            if other and Path(other).resolve() == target:
                raise ValueError("这个目录已分配给其他 Agent，请选择单独的目录")
        if data["workspace_mode"] == "dedicated":
            target.mkdir(parents=True, exist_ok=True)
        return str(target)

    def _project_agent(self, action, data):
        project, _identity, backend = project_agent_target(action, data)
        identifier(backend)
        existing = next((b for b in self.connector.bindings() if b["backend_project"] == backend), None)
        creating = action == "project-agent-create"
        if creating:
            if existing:
                raise ValueError("项目 Agent 已存在")
        elif not existing or existing["project_id"] != project:
            raise ValueError("找不到这个项目 Agent 的本机绑定")
        self._idle(backend)
        result = {"project_id": project, "backend_project": backend}
        if action == "project-agent-delete":
            self.connector.save_binding(
                dict(existing, assigned=False, enabled=False, project_scoped=True, project_deleted=True)
            )
            return dict(result, enabled=False)
        if existing and existing.get("project_deleted"):
            raise ValueError("项目 Agent 已经移除")
        requested = data["settings"]
        if existing and existing.get("model_config_version") == 2 and requested.get("model_config_version") != 2:
            raise ValueError("请更新应用后修改此项目 Agent 的模型设置")
        settings = self._settings(requested)
        model, effort = settings["model"], settings["effort"]
        from agent_harness import executors

        if settings["enabled"] and executors.model_kind(self.root, model) is None:
            raise ValueError("执行机器的可用模型表中没有 " + model)
        if settings.get("model_config_version") == 2 and effort != "default":
            from agent_harness.model_capabilities import for_model

            capability = for_model(self.root, model)
            if not capability or capability["efforts"] is None:
                raise ValueError("无法验证该模型的思考强度；请选择执行器默认")
            if effort not in capability["efforts"]:
                raise ValueError("执行机器上的该模型不支持思考强度 " + effort)
        name = data["name"].strip()
        workspace = None
        if creating:
            workspace = self._agent_workspace(data, backend) if data.get("workspace_mode") else self._workspace(project)
        self._write_project_settings(backend, settings, workspace)
        self.connector.save_binding(
            {
                **(existing or {}),
                **({"agent_workspace": True} if data.get("workspace_mode") else {}),
                **result,
                **settings,
                "name": name,
                "assigned": True,
                "project_scoped": True,
            }
        )
        return dict(result, name=name, settings=settings, enabled=settings["enabled"])

    def _run(self, action, data):
        if not isinstance(data, dict):
            raise ValueError("Invalid command data")
        if action == "browse":
            return self._browse(data.get("path"))
        if action == "workspace-configure":
            return self._configure_workspace(data)
        if action == "workspace-default":
            return self._configure_default_workspace(data)
        if action == "transfer-receive-directory":
            return self._configure_receive_directory(data)
        if action == "model-capabilities" and not data:
            from agent_harness import executors
            from agent_harness.model_capabilities import snapshot

            override = executors.settings(self.root).get("model")
            return {
                "models": snapshot(self.root),
                "default_executor": executors.selected(self.root),
                "legacy_model_override": (override.strip() or None) if isinstance(override, str) else None,
            }
        if action in PROJECT_AGENT_ACTIONS:
            return self._project_agent(action, data)
        if action not in {"create", "configure", "dispatch", "withdraw", "delete"}:
            raise ValueError("Unsupported owner action")
        employee = data.get("id")
        if not isinstance(employee, str) or not 1 <= len(employee) <= 120:
            raise ValueError("Invalid employee id")
        employees = self._employees()
        current = employees.get(employee)
        if action == "create":
            if current:
                raise ValueError("Employee already exists")
            name = data.get("name")
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120:
                raise ValueError("Invalid employee name")
            current = {
                "id": employee,
                "name": name.strip(),
                "settings": {"enabled": False},
            }
            employees[employee] = current
            self._save(employees)
            return current
        if action == "delete":
            bindings = self._bindings(employee)
            for binding in bindings:
                self._idle(binding["backend_project"])
            for binding in bindings:
                self.connector.save_binding(dict(binding, enabled=False, assigned=False))
            if current:
                current["deleted"] = True
                current["settings"]["enabled"] = False
                self._save(employees)
            return {"id": employee, "deleted": True}
        if not current or current.get("deleted"):
            raise ValueError("Unknown local employee")
        if action == "configure":
            name = data.get("name", current["name"])
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120:
                raise ValueError("Invalid employee name")
            settings = self._settings(data["settings"])
            bindings = self._bindings(employee)
            for binding in bindings:
                self._idle(binding["backend_project"])
            for binding in bindings:
                self.connector.executor(
                    {
                        "action": "configure",
                        "project": binding["backend_project"],
                        **{k: settings[k] for k in ("model", "effort", "timeout", "strict_worktree")},
                    }
                )
                self._remember_network(binding["backend_project"], settings["network"])
                updated = dict(
                    binding,
                    **{
                        k: settings[k]
                        for k in (
                            "model",
                            "effort",
                            "timeout",
                            "stages",
                            "strict_worktree",
                            "attachment_policy",
                            "network",
                        )
                    },
                    name=data.get("name", current["name"]),
                )
                updated["enabled"] = settings["enabled"] and binding.get("assigned", True)
                self.connector.save_binding(updated)
            current.update(settings=settings, name=name.strip())
            self._save(employees)
            return current
        project_id = data.get("project_id")
        if not isinstance(project_id, str) or not 1 <= len(project_id) <= 120:
            raise ValueError("Invalid shared project")
        backend = execution_project(employee, project_id)
        existing = next(
            (b for b in self.connector.bindings() if b["backend_project"] == backend),
            None,
        )
        if existing and existing.get("project_scoped"):
            raise ValueError("该 Agent 已由项目单独管理，请在项目内修改")
        if action == "withdraw":
            if existing:
                self.connector.save_binding(dict(existing, enabled=False, assigned=False))
            return {
                "id": employee,
                "project_id": project_id,
                "backend_project": backend,
                "enabled": False,
                "existing_jobs": "continue to completion",
            }
        settings = self._settings(current["settings"])
        workspace = self._workspace(project_id)
        self._idle(backend)
        self._write_project_settings(backend, settings, workspace)
        binding = {
            "assigned": True,
            "employee_id": employee,
            "project_id": project_id,
            "backend_project": backend,
            "name": current["name"],
            **{
                k: settings[k]
                for k in (
                    "model",
                    "effort",
                    "timeout",
                    "stages",
                    "enabled",
                    "strict_worktree",
                    "attachment_policy",
                    "network",
                )
            },
        }
        self.connector.save_binding(binding)
        return {
            "id": employee,
            "project_id": project_id,
            "backend_project": backend,
            "enabled": settings["enabled"],
        }

    def _browse(self, path):
        if path is None:
            roots = [Path(chr(letter) + ":\\") for letter in range(65, 91)] if os.name == "nt" else [Path("/")]
            return {
                "path": None,
                "parent": None,
                "children": [{"name": str(p), "path": str(p)} for p in roots if p.is_dir()],
            }
        if not isinstance(path, str):
            raise ValueError("Invalid folder path")
        folder = Path(path)
        if not folder.is_absolute() or not folder.is_dir():
            raise ValueError("Choose an existing absolute directory")
        folder = folder.resolve()
        children = []
        for child in sorted(folder.iterdir(), key=lambda p: p.name.casefold()):
            try:
                if child.is_dir():
                    children.append({"name": child.name, "path": str(child)})
            except OSError:
                continue
            if len(children) == 100:
                break
        return {
            "path": str(folder),
            "parent": str(folder.parent) if folder.parent != folder else None,
            "children": children,
        }
