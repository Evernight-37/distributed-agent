"""Apply authenticated owner controls locally, without a shell or credential access."""

import hashlib
import os
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
        allowed = {"remote_path", "model", "effort", "timeout", "stages", "enabled", "strict_worktree"}
        if not isinstance(settings, dict) or set(settings) - allowed:
            raise ValueError("Unsupported employee setting")
        checked = validate_project(dict(settings, name="owner-validation"))
        stages = settings.get("stages", ["discuss"])
        if not isinstance(stages, list) or not stages or set(stages) - {"discuss", "review", "implement"}:
            raise ValueError("Invalid employee stages")
        if not isinstance(settings.get("enabled", False), bool):
            raise ValueError("enabled must be boolean")
        if not isinstance(settings.get("strict_worktree", False), bool):
            raise ValueError("strict_worktree must be boolean")
        return {k: checked[k] for k in ("remote_path", "model", "effort")} | {
            "timeout": checked.get("timeout", 1800),
            "stages": stages,
            "enabled": settings.get("enabled", False),
            "strict_worktree": settings.get("strict_worktree", False),
        }

    def _run(self, action, data):
        if not isinstance(data, dict):
            raise ValueError("Invalid command data")
        if action == "browse":
            return self._browse(data.get("path"))
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
                project = read_json(self.root / "projects" / binding["backend_project"] / "project.json")
                if not project or Path(project["remote_path"]).resolve() != Path(settings["remote_path"]).resolve():
                    raise ValueError(
                        "A dispatched employee directory cannot change; withdraw and use a new employee to preserve project history"
                    )
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
        self._idle(backend)
        if not (self.root / "projects" / backend / "project.json").exists():
            self.connector.executor(
                {
                    "action": "register",
                    "project": dict(
                        name=backend,
                        **{k: settings[k] for k in ("remote_path", "model", "effort", "timeout", "strict_worktree")},
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
            if Path(registered["remote_path"]).resolve() != Path(settings["remote_path"]).resolve():
                raise ValueError("Existing project directory differs; preserve its session")
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
