"""Apply authenticated owner controls locally, without a shell or credential access."""

import hashlib
import os
import re
from pathlib import Path
from agent_harness.account_identity import verify_owner_command
from agent_harness.common import lock, read_json, write_json, validate_project
from agent_harness.owner_backend import canonical

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


def execution_project(employee, project):
    return "e-" + key(employee + "\0" + project)[:56]


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
                result = self._run(body["action"], body["data"])
                response = {"state": "applied", "result": result}
            except Exception as exc:
                response = {"state": "failed", "result": {"error": str(exc)}}
            write_json(
                receipt,
                {"fingerprint": fingerprint, "phase": "done", "response": response},
            )
            return response

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
        bindings = [b for b in self.connector.bindings() if b.get("project_id") == project_id]
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

        bindings = [b for b in self.connector.bindings() if b.get("project_id") == project_id]
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
            raise ValueError("Choose an existing absolute directory")
        path = Path(remote)
        if not path.is_absolute() or path.is_symlink() or not path.is_dir():
            raise ValueError("Choose an existing absolute directory, not a symbolic link")
        resolved = path.resolve()
        if resolved == Path(resolved.anchor):
            raise ValueError("Choose an existing absolute directory, not a filesystem root")
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

    def _bindings(self, employee):
        return [b for b in self.connector.bindings() if b.get("employee_id") == employee]

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
        allowed = {"remote_path", "model", "effort", "timeout", "stages", "enabled", "strict_worktree"}
        if not isinstance(settings, dict) or set(settings) - allowed:
            raise ValueError("Unsupported employee setting")
        model = settings.get("model")
        if not isinstance(model, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]+", model):
            raise ValueError("An explicit model is required")
        effort = settings.get("effort")
        if effort not in {"low", "medium", "high", "xhigh", "max", "ultra"}:
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
        return {"model": model, "effort": effort} | {
            "timeout": timeout,
            "stages": stages,
            "enabled": settings.get("enabled", False),
            "strict_worktree": settings.get("strict_worktree", False),
        }

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
                updated = dict(
                    binding,
                    **{k: settings[k] for k in ("model", "effort", "timeout", "stages", "strict_worktree")},
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
            (b for b in self._bindings(employee) if b["backend_project"] == backend),
            None,
        )
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
        if not (self.root / "projects" / backend / "project.json").exists():
            self.connector.executor(
                {
                    "action": "register",
                    "project": dict(
                        name=backend,
                        remote_path=workspace,
                        **{k: settings[k] for k in ("model", "effort", "timeout", "strict_worktree")},
                    ),
                    "memory": {
                        "revision": 0,
                        "facts": [],
                        "decisions": [],
                        "open_questions": [],
                    },
                }
            )
        else:
            registered = read_json(self.root / "projects" / backend / "project.json")
            if Path(registered["remote_path"]).resolve() != Path(workspace).resolve():
                raise ValueError("Existing project directory differs; choose the project workspace again")
            self.connector.executor(
                {
                    "action": "configure",
                    "project": backend,
                    **{k: settings[k] for k in ("model", "effort", "timeout", "strict_worktree")},
                }
            )
        binding = {
            "assigned": True,
            "employee_id": employee,
            "project_id": project_id,
            "backend_project": backend,
            "name": current["name"],
            **{k: settings[k] for k in ("model", "effort", "timeout", "stages", "enabled", "strict_worktree")},
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
