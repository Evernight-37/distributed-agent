"""Locally owned Node execution boundary."""

import os
import subprocess
import sys
from pathlib import Path

from agent_harness.api_contracts import NODE_EXECUTION
from agent_harness.common import events, identifier, lock, read_json, tail, validate_project, write_json
from agent_harness.health import HealthCode, component, normalize_exception, report


class NodeExecutionAPI:
    def __init__(self, handler, execution_handler=None, action_groups=()):
        self.handler = handler
        self.execution_handler = execution_handler
        self.action_groups = tuple(action_groups)

    def dispatch(self, request):
        request = NODE_EXECUTION.validate_request(request)
        if request["action"].startswith("execution_"):
            if self.execution_handler is None:
                raise RuntimeError("Node execution handler is not configured")
            return self.execution_handler(request)
        for group in self.action_groups:
            if request["action"] in group.actions:
                return group.dispatch(request)
        return self.handler(request)

    @staticmethod
    def contract():
        return NODE_EXECUTION.describe()


class NodeLocalActions:
    """Stateless diagnostics and local bootstrap actions."""

    actions = frozenset({"probe", "mkdir_demo", "health"})

    def __init__(self, root, cli_resolver):
        self.root = Path(root)
        self.cli_resolver = cli_resolver

    def dispatch(self, request):
        if request["action"] == "mkdir_demo":
            path = self.root / "demo-project"
            path.mkdir(exist_ok=True)
            return {"path": str(path)}
        if request["action"] == "health":
            components = [component("receiver", "healthy", HealthCode.OK, "Node API 可响应")]
            try:
                exe = self.cli_resolver()
                subprocess.check_output(
                    [exe, "--version"],
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=10,
                )
                components.append(component("codex", "healthy", HealthCode.OK, "Codex CLI 可运行"))
            except Exception as exc:
                components.append(
                    normalize_exception(
                        "codex",
                        exc,
                        code=HealthCode.CLI_NOT_FOUND,
                        message="Codex CLI 不可用",
                    )
                )
            return report(components)
        exe = self.cli_resolver()
        return {
            "hostname": os.environ.get("COMPUTERNAME"),
            "python": sys.executable,
            "cli": exe,
            "version": subprocess.check_output(
                [exe, "--version"], text=True, encoding="utf-8", errors="replace"
            ).strip(),
        }


class NodeProjectActions:
    """Project registration and configuration state, separate from process launch."""

    actions = frozenset({"project", "register", "configure", "memory"})

    def __init__(self, root, reconcile):
        self.root = Path(root)
        self.reconcile = reconcile

    def _project(self, name):
        return self.root / "projects" / identifier(name)

    def dispatch(self, request):
        action = request["action"]
        if action == "project":
            directory = self._project(request["project"])
            with lock(self.root / "registry.lock"):
                project = read_json(directory / "project.json")
                if not project:
                    raise ValueError("Project not registered")
                return {
                    "project": project,
                    "memory": read_json(directory / "memory.json"),
                    "session": read_json(directory / "session.json"),
                }
        if action == "register":
            data = validate_project(request["project"])
            directory = self._project(data["name"])
            with lock(self.root / "registry.lock"):
                if (directory / "project.json").exists():
                    raise ValueError("Project already registered; do not replace an existing session")
                write_json(directory / "project.json", data)
                write_json(directory / "session.json", {"thread_id": None})
                write_json(directory / "memory.json", request["memory"])
            return {"registered": data["name"]}
        directory = self._project(request["project"])
        with lock(self.root / "registry.lock"):
            project = read_json(directory / "project.json")
            if not project:
                raise ValueError("Project not registered")
            active = self.reconcile(read_json(directory / "active.json"))
            write_json(directory / "active.json", active)
            if active:
                raise ValueError("Project already has an active job: " + active["job"])
            if action == "configure":
                for key in ("model", "effort", "timeout"):
                    project[key] = request[key]
                if "strict_worktree" in request:
                    project["strict_worktree"] = request["strict_worktree"]
                validate_project(project)
                write_json(directory / "project.json", project)
                return {
                    "project": project,
                    "applies": "next turn; existing session retained",
                }
            old = read_json(directory / "memory.json")
            if request["expected_revision"] != old["revision"]:
                raise ValueError("Memory revision conflict")
            memory = request["memory"]
            if memory["revision"] != old["revision"] + 1:
                raise ValueError("Memory revision must increment exactly once")
            write_json(directory / "memory.json", memory)
            return {"revision": memory["revision"]}


class NodeJobActions:
    """Observe and cancel durable jobs without owning process launch."""

    actions = frozenset({"status", "cancel", "fetch"})

    def __init__(self, root, reconcile, terminal_states):
        self.root = Path(root)
        self.reconcile = reconcile
        self.terminal_states = frozenset(terminal_states)

    def _job(self, job):
        return self.root / "jobs" / identifier(job)

    def _project(self, project):
        return self.root / "projects" / identifier(project)

    def dispatch(self, request):
        action = request["action"]
        path = self._job(request["job"])
        state = read_json(path / "state.json")
        if not state:
            raise ValueError("Unknown job")
        directory = self._project(state["project"])
        with lock(self.root / "registry.lock"):
            active = read_json(directory / "active.json")
            owned = bool(active and active["job"] == request["job"])
            if owned:
                active = self.reconcile(active)
                write_json(directory / "active.json", active)
            else:
                current_state = read_json(path / "state.json", {})
                if current_state.get("status") not in self.terminal_states:
                    current_state.update(
                        status="interrupted",
                        finished_at=_utc_now(),
                        error="No active worker owns this non-terminal job; start a new turn explicitly",
                    )
                    write_json(path / "state.json", current_state)
            state = read_json(path / "state.json")
            if action == "cancel" and state["status"] not in self.terminal_states:
                write_json(path / "cancel.json", {"requested_at": _utc_now()})
        cancel = read_json(path / "cancel.json")
        if cancel:
            state["cancel_requested_at"] = cancel["requested_at"]
        state["session"] = read_json(directory / "session.json")
        if action == "fetch":
            state["report"] = tail(path / "report.txt", 262144)
            state["stderr"] = tail(path / "stderr.log")
            state["supervisor_log"] = tail(path / "supervisor.log")
            request_config = read_json(path / "request.json", {}) or {}
            if request_config.get("managed_turn"):
                state["app_server_stderr"] = tail(directory / "app-server-stderr.log", 32768)
                state["warm_supervisor_log"] = tail(directory / "warm-supervisor.log", 32768)
        state["recent_events"] = events(tail(path / "events.jsonl"))[-30:]
        return state


def _utc_now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
