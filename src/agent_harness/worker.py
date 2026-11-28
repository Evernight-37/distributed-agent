"""Detached Codex worker with a small local JSON-RPC surface."""

import base64
import ctypes
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from agent_harness import executors
from agent_harness.common import (
    events,
    identifier,
    lock,
    outcome,
    read_json,
    referenced_message_ids,
    tail,
    validate_project,
    write_json,
)
from agent_harness.node_api import NodeExecutionAPI, NodeJobActions, NodeLocalActions, NodeProjectActions
from agent_harness.usage import CliUsage

ROOT = Path(__file__).resolve().parent
TERMINAL = {"needs_review", "failed", "cancelled", "timed_out", "interrupted"}
PROCESS_MISSING_CONFIRM_SECONDS = 5.0


def now():
    return datetime.now(timezone.utc).isoformat()


def project_dir(name):
    return ROOT / "projects" / identifier(name)


def job_dir(job):
    return ROOT / "jobs" / identifier(job)


def execution_timeout(config):
    """Return the validated per-project timeout for every turn type."""
    timeout = config["project"]["timeout"]
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValueError("Project timeout must be a positive number")
    return timeout


def process_stamp(pid):
    """Creation timestamp + alive state prevents confusing a recycled PID."""
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            proc = Path("/proc") / str(pid) / "stat"
            if proc.is_file():
                return proc.read_text().rsplit(")", 1)[1].split()[19]
            return subprocess.check_output(["ps", "-p", str(pid), "-o", "lstart="], text=True).strip() or None
        except OSError:
            return None
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    handle = kernel.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return None
    try:
        code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:
            return None
        stamps = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in stamps)):
            return None
        return str((stamps[0].dwHighDateTime << 32) | stamps[0].dwLowDateTime)
    finally:
        kernel.CloseHandle(handle)


def codex_version(exe):
    """Probe a candidate without making a model request."""
    return executors.version(exe)


def set_root(root):
    """Bind this loaded worker module to one explicit receiver state root."""
    global ROOT
    ROOT = Path(root).resolve()
    if not ROOT.is_dir():
        raise ValueError("Worker state root does not exist")
    return ROOT


def executor_environment(root=None, stage=None, full_access=False, network=None, kind=None):
    """Build the exact environment used by the local executor and its login preflight."""
    root = ROOT if root is None else Path(root)
    return executors.child_environment(root, stage, full_access, network, kind)


def codex_login_status(exe, root=None, kind=None, *, network=None):
    """Check the executor that will run this turn, without reading credentials.

    The historical name is kept because the worker, the desktop settings API and
    the Linux receiver all call it through this module.
    """
    root = ROOT if root is None else Path(root)
    return executors.login(root, exe, kind, network=network)


def cli_status(root=None):
    """Find a working local-executor CLI in the worker account."""
    root = ROOT if root is None else Path(root)
    # Pass this module's probe so callers and tests keep a single override point.
    return executors.status(root, codex_version)


def cli_path(root=None, kind=None):
    root = ROOT if root is None else Path(root)
    result = cli_status(root) if kind is None else executors.status_of(root, kind, codex_version)
    if not result["found"]:
        raise RuntimeError(result["error"])
    return result["path"]


def launch_supervisor(job):
    """Launch a standalone one-shot worker independently of its caller."""
    argv = [sys.executable, "-X", "utf8", str(ROOT / "worker.py"), "run", job]
    output_path = job_dir(job) / "supervisor.log"
    if os.name != "nt":
        with output_path.open("ab") as output:
            child = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=output,
                start_new_session=True,
                close_fds=True,
            )
        return child.pid
    import base64

    command = subprocess.list2cmdline(argv)
    quoted = "'" + command.replace("'", "''") + "'"
    script = "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; "
    script += "$startup=New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ShowWindow=[uint16]0}; "
    script += (
        "$result=Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine="
        + quoted
        + ";ProcessStartupInformation=$startup}; "
    )
    script += "$result | Select-Object ReturnValue,ProcessId | ConvertTo-Json -Compress"
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-EncodedCommand", encoded],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        response = json.loads(result.stdout)
    except ValueError:
        raise RuntimeError("Hidden WMI launch failed: " + result.stderr[-1000:])
    if result.returncode or response.get("ReturnValue") != 0:
        raise RuntimeError("Hidden WMI launch rejected: " + str(response))
    return int(response["ProcessId"])


class ManagedRuntime:
    """Own warm worker handles inside the long-lived desktop receiver."""

    def __init__(self, root):
        self.root = set_root(root)
        self._children = {}
        self._guard = threading.Lock()

    def rpc(self, request):
        return rpc(request, self)

    def launch(self, project, token, generation):
        directory = project_dir(project)
        output_path = directory / "warm-supervisor.log"
        # Code is immutable for the lifetime of this receiver. State lives in
        # self.root and is passed explicitly; never execute a file that an app
        # upgrade may overwrite underneath an active process.
        argv = [
            sys.executable,
            "-X",
            "utf8",
            str(Path(__file__).resolve().parent / "warm_worker.py"),
            "--root",
            str(self.root),
            "--project",
            project,
            "--token",
            token,
            "--generation",
            generation,
        ]
        flags = 0
        kwargs = {"cwd": self.root, "stdin": subprocess.DEVNULL, "close_fds": True}
        if os.name == "nt":
            flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            kwargs["creationflags"] = flags
        else:
            kwargs["start_new_session"] = True
        output = output_path.open("ab")
        try:
            child = subprocess.Popen(argv, stdout=output, stderr=output, **kwargs)
        except Exception:
            output.close()
            raise
        with self._guard:
            self._children[generation] = child
        thread = threading.Thread(
            target=self._watch,
            args=(project, generation, child, output),
            name="managed-worker-" + generation[:8],
            daemon=True,
        )
        thread.start()
        return child

    def _watch(self, project, generation, child, output):
        code = child.wait()
        output.close()
        directory = project_dir(project)
        startup = read_json(directory / "startup-error.json", {}) or {}
        self_exit = read_json(directory / "worker-self-exit.json", {}) or {}
        evidence = {
            "generation": generation,
            "pid": child.pid,
            "exit_code": code,
            "finished_at": now(),
        }
        if self_exit.get("generation") == generation and self_exit.get("error"):
            evidence["error"] = self_exit.get("error")
        elif startup.get("generation") == generation:
            evidence["error"] = startup.get("error")
        with lock(ROOT / "registry.lock"):
            write_json(directory / "worker-exit.json", evidence)
            active = read_json(directory / "active.json")
            if active and active.get("generation") == generation:
                path = job_dir(active["job"])
                state = read_json(path / "state.json", {})
                if state.get("status") not in TERMINAL:
                    state.update(
                        status="failed",
                        finished_at=now(),
                        exit_code=code,
                        error=evidence.get("error") or "Managed worker exited before completing its active turn",
                    )
                    write_json(path / "state.json", state)
                write_json(directory / "active.json", None)
            warm = read_json(directory / "warm.json")
            if warm and warm.get("generation") == generation:
                write_json(directory / "warm.json", None)
        with self._guard:
            self._children.pop(generation, None)

    def child(self, generation):
        with self._guard:
            return self._children.get(generation)

    def owns(self, generation, pid):
        child = self.child(generation)
        return bool(child and child.pid == pid and child.poll() is None)

    def retire_idle_workers(self):
        """Revoke idle warm workers owned by this receiver before it exits."""
        retired = []
        with lock(ROOT / "registry.lock"):
            for directory in (ROOT / "projects").glob("*"):
                if not directory.is_dir() or read_json(directory / "active.json"):
                    continue
                warm = read_json(directory / "warm.json") or {}
                generation, pid = warm.get("generation"), warm.get("pid")
                if generation and isinstance(pid, int) and self.owns(generation, pid):
                    write_json(directory / "warm.json", None)
                    retired.append(generation)
        # Revocation is observed by the worker loop within 200 ms.  Do not
        # terminate a process by PID here; identity can be recycled and active
        # jobs are deliberately allowed to survive receiver shutdown.
        deadline = time.monotonic() + 3
        for generation in retired:
            child = self.child(generation)
            if not child:
                continue
            remaining = deadline - time.monotonic()
            if remaining > 0:
                try:
                    child.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    pass
        return len(retired)

    def retire_workers_after_turn(self):
        """Revoke every owned warm registration without interrupting active turns."""
        retired = []
        with lock(ROOT / "registry.lock"):
            for directory in (ROOT / "projects").glob("*"):
                if not directory.is_dir():
                    continue
                warm = read_json(directory / "warm.json") or {}
                generation, pid = warm.get("generation"), warm.get("pid")
                if generation and isinstance(pid, int) and self.owns(generation, pid):
                    # An active worker observes this only after run_job returns;
                    # an idle worker exits within its next 200 ms loop.
                    write_json(directory / "warm.json", None)
                    retired.append(generation)
        return len(retired)


def reconcile(active):
    if not active:
        return None
    path = job_dir(active["job"])
    state = read_json(path / "state.json", {})
    if state.get("status") in TERMINAL:
        return None
    directory = project_dir(state.get("project", "")) if state.get("project") else None
    generation = active.get("generation")
    evidence = (read_json(directory / "worker-exit.json", {}) or {}) if directory and generation else {}
    startup = (read_json(directory / "startup-error.json", {}) or {}) if directory and generation else {}
    self_exit = (read_json(directory / "worker-self-exit.json", {}) or {}) if directory and generation else {}
    # The worker's own exception is more specific than the guardian's generic
    # exit observation, and both records are generation scoped.
    if generation and self_exit.get("generation") == generation:
        state.update(
            status="failed",
            finished_at=now(),
            error=self_exit.get("error") or "Managed worker exited before completing its active turn",
        )
        write_json(path / "state.json", state)
        return None
    if generation and evidence.get("generation") == generation:
        state.update(
            status="failed",
            finished_at=now(),
            exit_code=evidence.get("exit_code"),
            error=evidence.get("error") or "Managed worker exited before completing its active turn",
        )
        write_json(path / "state.json", state)
        return None
    if generation and startup.get("generation") == generation:
        state.update(
            status="failed",
            finished_at=now(),
            error=startup.get("error") or "Managed worker startup failed",
        )
        write_json(path / "state.json", state)
        return None
    stamp = process_stamp(active["pid"])
    if stamp is None or stamp != active.get("stamp"):
        # A single Windows process query can transiently fail.  Persist one
        # unknown observation and require a later independent reconciliation.
        observed_at = time.time()
        observations = int(active.get("unknown_observations", 0))
        unknown_at = active.get("unknown_observed_at")
        # Two HTTP fetches in the same UI refresh are not independent process
        # observations. Require a real wall-clock interval before escalation.
        if observations < 1 or not isinstance(unknown_at, (int, float)):
            return dict(
                active,
                unknown_observations=1,
                unknown_since=now(),
                unknown_observed_at=observed_at,
            )
        if observed_at - unknown_at < PROCESS_MISSING_CONFIRM_SECONDS:
            return active
        state.update(
            status="interrupted",
            finished_at=now(),
            error="Worker could not be found in two consecutive reconciliations; resume session explicitly",
        )
        write_json(path / "state.json", state)
        return None
    active.pop("unknown_observations", None)
    active.pop("unknown_since", None)
    active.pop("unknown_observed_at", None)
    return active


def require_worktree(project):
    """Implementation needs a dedicated linked Git worktree, not the shared checkout."""
    path = Path(project["remote_path"])
    if not (path / ".git").is_file():
        raise ValueError("Implementation requires a dedicated linked Git worktree; register that directory first")
    import shutil

    git = shutil.which("git")
    if not git:
        fallback = Path(os.environ.get("ProgramFiles", "C:\\Program Files")) / "Git" / "cmd" / "git.exe"
        if fallback.is_file():
            git = str(fallback)
    if not git:
        raise ValueError("Git is required to verify worktree isolation")

    def value(*args):
        result = subprocess.run(
            [git, "-C", str(path), "rev-parse", *args],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode:
            raise ValueError("Git worktree validation failed")
        return result.stdout.strip()

    top = Path(value("--show-toplevel")).resolve()
    git_dir = Path(value("--absolute-git-dir")).resolve()
    common = Path(value("--path-format=absolute", "--git-common-dir")).resolve()
    if top != path.resolve() or git_dir == common:
        raise ValueError("Implementation directory is not the root of a linked Git worktree")
    return {"worktree": str(top), "git_dir": str(git_dir), "common_dir": str(common)}


def _git_executable():
    import shutil

    git = shutil.which("git")
    if not git:
        fallback = Path(os.environ.get("ProgramFiles", "C:\\Program Files")) / "Git" / "cmd" / "git.exe"
        if fallback.is_file():
            git = str(fallback)
    if not git:
        raise ValueError("Git is required for strict worktree mode")
    return git


def _worktree_status(root):
    """Return changed repo-relative paths from porcelain status."""
    result = subprocess.run(
        [_git_executable(), "-C", str(root), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        capture_output=True,
        timeout=30,
    )
    if result.returncode:
        raise ValueError("Git worktree status failed")
    chunks = result.stdout.decode("utf-8", errors="replace").split("\0")
    paths = []
    index = 0
    while index < len(chunks):
        chunk = chunks[index]
        index += 1
        if len(chunk) < 4:
            continue
        paths.append(chunk[3:].replace("\\", "/"))
        # Porcelain -z emits the destination as a second path for renames/copies.
        if any(flag in {"R", "C"} for flag in chunk[:2]) and index < len(chunks) and chunks[index]:
            paths.append(chunks[index].replace("\\", "/"))
            index += 1
    return sorted(set(paths))


def _strict_allowed_files(value):
    """Parse strict-mode allowlist: one repo-relative path per line."""
    if not isinstance(value, str):
        raise ValueError("Strict worktree mode requires one allowed file per line")
    result = []
    for raw in value.splitlines():
        item = raw.strip().replace("\\", "/")
        if not item:
            continue
        if item.startswith("/") or ":" in item or item == "." or item.startswith("../") or "/../" in item:
            raise ValueError("Strict worktree allowlist must contain repository-relative paths")
        directory = item.endswith("/")
        result.append(item.rstrip("/") + ("/" if directory else ""))
    if not result:
        raise ValueError("Strict worktree mode requires at least one allowed file")
    return sorted(set(result))


def _strict_guard(project, contract):
    if not project.get("strict_worktree") or contract.get("stage") != "implement":
        return None
    details = require_worktree(project)
    root = Path(details["worktree"])
    changed = _worktree_status(root)
    if changed:
        raise ValueError("Strict worktree must be clean before implementation: " + ", ".join(changed[:5]))
    return {
        "root": str(root),
        "head": subprocess.check_output(
            [_git_executable(), "-C", str(root), "rev-parse", "HEAD"], text=True, timeout=15
        ).strip(),
        "allowed_files": _strict_allowed_files(contract.get("allowed_files")),
    }


def _check_strict_guard(guard):
    if not guard:
        return None
    root = Path(guard["root"])
    head = subprocess.run(
        [_git_executable(), "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if head.returncode or head.stdout.strip() != guard.get("head"):
        return "Strict worktree HEAD changed during implementation"
    changed = _worktree_status(root)
    allowed = guard["allowed_files"]
    violations = [
        path
        for path in changed
        if not any(path == item or (item.endswith("/") and path.startswith(item)) for item in allowed)
    ]
    if violations:
        return "Strict worktree changed files outside allowed_files: " + ", ".join(violations[:10])
    return None


def _await_ready(directory, child, token, generation, timeout=10):
    """Wait for the child's explicit READY record; timeout only bounds a hang."""
    deadline = time.monotonic() + timeout
    changed = threading.Event()
    while True:
        ready = read_json(directory / "ready.json", {}) or {}
        if (
            ready.get("token") == token
            and ready.get("generation") == generation
            and ready.get("pid") == child.pid
            and ready.get("stamp")
            and process_stamp(child.pid) == ready.get("stamp")
        ):
            if child.poll() is not None:
                raise RuntimeError("Managed worker exited after READY")
            return ready
        startup = read_json(directory / "startup-error.json", {}) or {}
        if startup.get("token") == token and startup.get("generation") == generation:
            raise RuntimeError(startup.get("error") or "Managed worker startup failed")
        code = child.poll()
        if code is not None:
            raise RuntimeError("Managed worker exited before READY (exit code %s)" % code)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("Managed worker did not publish READY before the startup deadline")
        changed.wait(min(0.05, remaining))


def _cleanup_failed_managed_launch(directory, generation, child=None):
    """Remove only records owned by one failed launch generation."""
    if child is not None and child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)
    for name in ("active.json", "warm.json", "launch.json", "ready.json"):
        path = directory / name
        record = read_json(path, {}) or {}
        if record.get("generation") == generation:
            write_json(path, None)


def rpc(request, managed_runtime=None):
    from agent_harness.jobrunner import rpc as execution_rpc

    return NodeExecutionAPI(
        lambda validated: _rpc(validated, managed_runtime),
        lambda validated: execution_rpc(ROOT, validated),
        (
            NodeLocalActions(ROOT, cli_path),
            NodeProjectActions(ROOT, reconcile),
            NodeJobActions(ROOT, reconcile, TERMINAL),
        ),
    ).dispatch(request)


def _rpc(request, managed_runtime=None):
    action = request["action"]
    if action == "start":
        directory = project_dir(request["project"])
        with lock(ROOT / "registry.lock"):
            project = read_json(directory / "project.json")
            if not project:
                raise ValueError("Project not registered")
            active = reconcile(read_json(directory / "active.json"))
            write_json(directory / "active.json", active)
            if active:
                raise ValueError("Project already has an active job: " + active["job"])
            contract = json.loads(request["prompt"])
            current_stage = contract.get("stage", "discuss")
            # Read-only discussion/review turns may share a tree. Any implement
            # turn remains exclusive across identical or nested workspaces.
            for other in (ROOT / "projects").glob("*/project.json"):
                if other.parent == directory:
                    continue
                data = read_json(other)
                current_path = Path(project["remote_path"]).resolve()
                other_path = Path(data["remote_path"]).resolve()
                if (
                    current_path == other_path
                    or current_path in other_path.parents
                    or other_path in current_path.parents
                ):
                    other_active = reconcile(read_json(other.parent / "active.json"))
                    write_json(other.parent / "active.json", other_active)
                    if other_active and (
                        current_stage == "implement" or other_active.get("stage", "implement") == "implement"
                    ):
                        raise ValueError("Remote directory is busy in another project")
            validate_project(project)
            strict_guard = _strict_guard(project, contract)
            # A pull receiver uses a directory chosen and enabled by its
            # machine owner. Keep the linked-worktree requirement for legacy
            # centrally configured workers, while allowing local execution in
            # that selected workspace (still under Codex workspace-write).
            if contract.get("stage") == "implement" and (strict_guard or not project.get("local_control")):
                require_worktree(project)
            channel = request.get("session_channel")
            if contract.get("stage") == "implement" and isinstance(channel, str) and channel.startswith("work-"):
                # Structured project work owns its checkout across turns. A new
                # work item may take over only after the previous item leaves it
                # clean; legacy jobs retain their existing behaviour.
                try:
                    git = _git_executable()
                except ValueError:
                    git = None
                if git:
                    from agent_harness.workspace_ownership import claim_worktree

                    claim_worktree(ROOT, project["remote_path"], channel, git, _worktree_status)
            job = identifier(request["job"])
            path = job_dir(job)
            path.mkdir(parents=True, exist_ok=False)
            # The agent's model decides which local CLI runs this turn. A model the
            # table does not list falls back to the machine's own executor, so an
            # existing agent keeps working before its model is added to the table.
            kind = executors.kind_for(ROOT, project.get("model"))
            # Native sessions can remember the last explicit effort. Isolate
            # engine defaults so subsequent turns cannot inherit that override.
            native_default = project.get("effort") == "default"
            session_kind = kind + "-default" if native_default else kind
            session_file = "session-" + session_kind + ".json"
            if channel:
                identifier(channel)
                session_file = "channels/" + channel + "-" + session_kind + ".json"
            elif not native_default and not (directory / session_file).is_file():
                # Older installs kept one thread id for the single executor the
                # machine ran before routing existed; adopt it only for that one.
                legacy = directory / "session.json"
                if legacy.is_file() and kind == executors.selected(ROOT):
                    session_file = "session.json"
            session = read_json(directory / session_file, {"thread_id": None})
            config = {
                "project": project,
                "job": job,
                "prompt": request["prompt"],
                "memory": {} if channel else read_json(directory / "memory.json"),
                "thread_id": session["thread_id"],
                "session_file": session_file,
                "previous_job": session.get("last_job"),
                "managed_turn": bool(channel),
                "strict_guard": strict_guard,
            }
            write_json(path / "request.json", config)
            write_json(
                path / "state.json",
                {
                    "job": job,
                    "project": project["name"],
                    "status": "starting",
                    "created_at": now(),
                },
            )
            try:
                # Only Codex has a persistent app-server to keep warm. DSH runs
                # one process per turn and resumes through its session id, so its
                # managed turns take the ordinary supervisor path.
                warm_turns = bool(
                    config["managed_turn"] and project.get("local_control") and executors.MANAGED_TURNS.get(kind, False)
                )
                if warm_turns:
                    warm_path = directory / "warm.json"
                    warm = read_json(warm_path)
                    if (
                        warm
                        and warm.get("generation")
                        and warm.get("token")
                        and isinstance(warm.get("pid"), int)
                        and process_stamp(warm["pid"]) == warm.get("stamp")
                        and managed_runtime is not None
                        and managed_runtime.owns(warm.get("generation"), warm["pid"])
                    ):
                        pid, stamp = warm["pid"], warm["stamp"]
                        generation = warm.get("generation")
                    else:
                        if managed_runtime is None:
                            raise RuntimeError("Managed turns require the persistent desktop receiver")
                        # A live warm record from an older receiver generation
                        # must not make the new service keep executing obsolete
                        # code. Revocation lets it close its app-server safely.
                        if warm and not read_json(directory / "active.json"):
                            write_json(warm_path, None)
                        launch_token = uuid.uuid4().hex
                        generation = uuid.uuid4().hex
                        launch_path = directory / "launch.json"
                        parent_pid = os.getpid()
                        parent_stamp = process_stamp(parent_pid)
                        if parent_stamp is None:
                            raise RuntimeError("Parent process is not alive")
                        write_json(directory / "ready.json", None)
                        write_json(directory / "startup-error.json", None)
                        write_json(directory / "worker-self-exit.json", None)
                        write_json(
                            launch_path,
                            {
                                "token": launch_token,
                                "generation": generation,
                                "project": project["name"],
                                "root": str(ROOT),
                                "parent_pid": parent_pid,
                                "parent_stamp": parent_stamp,
                            },
                        )
                        child = None
                        try:
                            child = managed_runtime.launch(project["name"], launch_token, generation)
                            ready = _await_ready(directory, child, launch_token, generation)
                            pid, stamp = ready["pid"], ready["stamp"]
                            write_json(
                                warm_path,
                                {
                                    "pid": pid,
                                    "stamp": stamp,
                                    "token": launch_token,
                                    "generation": generation,
                                },
                            )
                            write_json(
                                directory / "active.json",
                                {
                                    "job": job,
                                    "pid": pid,
                                    "stamp": stamp,
                                    "generation": generation,
                                    "stage": current_stage,
                                },
                            )
                            write_json(launch_path, None)
                        except Exception:
                            _cleanup_failed_managed_launch(directory, generation, child)
                            raise
                else:
                    pid = launch_supervisor(job)
                    stamp = process_stamp(pid)
                if stamp is None:
                    raise RuntimeError("Supervisor failed to start")
            except Exception as exc:
                state = read_json(path / "state.json")
                state.update(status="failed", error=str(exc), finished_at=now())
                write_json(path / "state.json", state)
                raise
            # A completed warm turn deliberately leaves active.json as JSON
            # null.  read_json's default only applies when the file is absent,
            # so normalize that valid idle value before checking whether the
            # fresh launch already committed its ownership record.
            committed_active = read_json(directory / "active.json", {}) or {}
            if not (warm_turns and committed_active.get("job") == job):
                active_record = {"job": job, "pid": pid, "stamp": stamp, "stage": current_stage}
                if warm_turns and generation:
                    active_record["generation"] = generation
                write_json(directory / "active.json", active_record)
            return {"job": job, "status": "starting", "supervisor_pid": pid}
    raise ValueError("Unknown action")


PEER_RULES = """You are an independent peer collaborating with the coordinating application on another machine.
Use the tools your executor provides. The stage in the task contract states what the humans expect, not what you may do.
You may spawn your own sub-agents when that helps; you are responsible for the combined result you report.
If the sandbox denies something you need, say what you need and why in your report instead of assuming the task is impossible.
Report commands, observations, changed files, validation, disagreements and remaining risks.
Do not read credentials or contact outside parties unless this task explicitly authorizes it.
Your final report awaits independent review; you cannot approve your own work.
"""

GROUP_RULES = """You are an agent in a human-owned project group. Use the visible messages, shared project memory, the task contract and the assigned local project directory.
If a foolship_reply tool is available, call it once with the structured result instead of writing JSON. Otherwise reply with one JSON object: {"reply": str, "messages": [{recipient, body}], "attachments": [{path}], "tasks": [...], "executions": [...], "memory_notes": [str], "work_proposal": object|null, "work_delivery": object|null, "done": bool}.
- reply is the human-facing answer, without private reasoning or progress logs. messages are for distinct follow-ups only and never repeat reply.
- attachments are project-relative paths: existing ordinary files inside the project, at most five, each at most 5 MB; never hidden files, credentials or paths outside it.
- memory_notes only for durable facts from the public group; empty in a private turn.
- work_proposal is only a draft for explicit human confirmation, never authorization to start work. Use it only as the project administrator in a human-directed turn. Include the complete title, instructions, deliverable, acceptance, validation, agent, allowed_files, stage, visibility and reference_message_ids. For a new draft omit id and revision or set both to null. The Hub assigns them and reports whether the draft was saved. For a change use the existing id and current base revision supplied in the context. The Hub increments the version.
- work_follow_up identifies one existing work item when the human explicitly asks for further action in that same scope. Use only an unambiguous work_id supplied in context; do not rewrite the human request or broaden the work.
- work_delivery is only for a scheduled project-work execution. Put the actual requested result in work_delivery.summary even for a one-line result, use an empty links list when there are no links, and leave message_id null. The chat reply alone cannot register a delivery. Do not create unrelated tasks, executions or messages from that scoped turn.
Use the tools your executor gives you. Your task stage describes its intent; the executor and machine owner decide what is permitted. You may spawn your own sub-agents and remain responsible for the combined result. If this task needs extra execution permissions, put request:{"reason": "..."} in the reply. The machine owner can approve one continuation of this task.
Never reveal a human's private message outside its conversation. Do not read credentials or contact outside parties unless the task says so.
"""

# Every resumed turn still needs the reply contract, even though the full rules
# are only sent once per thread.
REPLY_REMINDER = "Reply with one JSON object using the full structured reply contract; include work_proposal or work_delivery only when this turn calls for it."


def reply_rules(policy, first_turn, *, project_tools=False, file_delivery=False):
    base = GROUP_RULES if first_turn else REPLY_REMINDER
    if policy is None:
        return base
    if first_turn:
        lines = GROUP_RULES.splitlines()
        lines[1] = (
            "Use foolship_reply if offered. Otherwise return the structured final response defined by the runtime."
        )
        base = "\n".join(line for line in lines if not line.startswith("- work_"))
    else:
        base = "Return the structured final response defined by the runtime."
    if project_tools and policy["role"] == "admin":
        return base + (
            "\nUse the available task tools for formal assignments. Drafts require human confirmation. "
            "For revisions send only changed fields. After success leave project_action null."
        )
    if project_tools:
        tool_rules = (
            "\nUse the registered project tools to save a draft, revise a draft, follow up or submit a delivery. "
            "Only use tools offered for this turn. For a revision send only the changed fields; "
            "the system chooses the base version. Check tool errors and correct the inputs before retrying."
        )
        if file_delivery:
            return (
                base
                + tool_rules
                + (
                    " Use project_task_deliver for every delivery. Put web URLs in links "
                    "and project-relative file paths in attachments. For file deliveries, "
                    "pending_attachment_delivery means the receiver accepted the request and will handle approval and upload "
                    "after this turn. Never repeat it in chat or in the final response. "
                    "After a successful tool call, finish normally with project_action null and empty attachments. "
                    "Only use project_action wait when you need a human decision."
                )
            )
        return (
            base
            + tool_rules
            + (
                " For a text or link delivery call the project_task_deliver tool before the final response. "
                "The final response cannot register text or link deliveries. Once a tool "
                "reports success, do not repeat the operation or its delivery in chat. Leave project_action null in "
                "the final response except when the execution needs a human decision (wait) or is submitting "
                "project files as attachments (deliver with attachments, using the file approval workflow)."
            )
        )
    return base + (
        "\nThis turn uses the strict project_action protocol. Return project_action as the role-specific "
        "action or null; do not return work_proposal, work_follow_up or work_delivery. Legacy tasks must be empty. "
        "For creation and delivery, provide content only; trusted systems assign mutable identifiers and versions."
    )


def unfenced_report(text):
    """Unwrap a JSON reply the executor put in a markdown code fence.

    Codex returns the contract as bare JSON; the DeepSeek Harness CLI wraps the
    same object in ```json fences. Left alone, the coordinator cannot parse it and
    publishes the raw JSON as the answer.
    """
    value = (text or "").strip()
    if not value.startswith("```") or value.count("```") < 2:
        return value
    lines = value.splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    inner = "\n".join(lines).strip()
    return inner if inner.startswith("{") and inner.endswith("}") else value


def build_command(exe, project, thread_id):
    """Codex argv for one turn; a managed turn adds its schema separately."""
    return executors.codex_command(exe, project, thread_id)


def effective_model(thread_id, since=None, *, kind=None):
    """The model the executor actually used, when it records one."""
    return executors.effective_model(kind or executors.selected(ROOT), thread_id, since)


def final_action_schema(policy, project_tools=False):
    from agent_harness.project_actions import action_schema

    schema = action_schema(policy)
    if not project_tools:
        return schema
    branches = [
        branch
        for branch in schema.get("anyOf", [schema])
        if branch["type"] == "null" or branch["properties"]["kind"]["enum"][0] in {"wait", "deliver"}
    ]
    return branches[0] if len(branches) == 1 else {"anyOf": branches}


def project_delivery_error(report):
    """Keep text deliveries on the native tool path for tool-enabled turns."""
    try:
        answer = json.loads(report)
    except (TypeError, ValueError):
        return None
    action = answer.get("project_action") if isinstance(answer, dict) else None
    if not isinstance(action, dict) or action.get("kind") != "deliver":
        return None
    files = answer.get("attachments")
    if not isinstance(files, list) or not files:
        return "Text or link delivery must use project_task_deliver; final deliver requires file attachments."
    return None


def reply_schema(policy=None, *, project_tools=False):
    """Structured reply contract for managed group-chat turns."""

    def obj(properties):
        return {
            "type": "object",
            "additionalProperties": False,
            "required": list(properties),
            "properties": properties,
        }

    def array(properties):
        return {"type": "array", "items": obj(properties)}

    def optional_object(properties):
        return {"anyOf": [obj(properties), {"type": "null"}]}

    proposal = optional_object(
        {
            "id": {"type": ["string", "null"]},
            "revision": {"type": ["integer", "null"]},
            **{
                key: {"type": "string"}
                for key in (
                    "title",
                    "instructions",
                    "deliverable",
                    "acceptance",
                    "validation",
                    "agent",
                    "allowed_files",
                )
            },
            "stage": {"type": "string", "enum": ["discuss", "implement"]},
            "visibility": {"type": "string", "enum": ["group", "private"]},
            "reference_message_ids": {"type": "array", "items": {"type": "integer"}},
        }
    )
    delivery = optional_object(
        {
            "summary": {"type": "string"},
            "links": {"type": "array", "items": {"type": "string"}},
            "message_id": {"type": ["integer", "null"]},
        }
    )

    properties = {
        "reply": {"type": "string"},
        "attachments": array({"path": {"type": "string"}}),
        "done": {"type": "boolean"},
        "messages": array(
            {
                "recipient": {"type": ["string", "null"]},
                "body": {"type": "string"},
            }
        ),
        "tasks": array(
            {
                key: {"type": "string"}
                for key in (
                    "agent",
                    "title",
                    "instructions",
                    "stage",
                    "allowed_files",
                    "acceptance",
                    "validation",
                )
            }
        ),
        "executions": array({"template": {"type": "string"}}),
        "memory_notes": {"type": "array", "items": {"type": "string"}},
        "work_proposal": proposal,
        "work_follow_up": optional_object({"work_id": {"type": "string"}}),
        "work_delivery": delivery,
        # A request the human answers with /allow or /deny.
        "request": optional_object({"reason": {"type": "string"}, "scope": {"type": ["string", "null"]}}),
    }
    if policy is not None:
        properties["tasks"] = {"type": "array", "items": properties["tasks"]["items"], "maxItems": 0}
        for legacy in ("work_proposal", "work_follow_up", "work_delivery"):
            properties.pop(legacy)
        properties["project_action"] = final_action_schema(policy, project_tools)
    return obj(properties)


FOOLSHIP_REPLY_JS = """\
// In-process reply tool for a Foolship turn. The agent answers by calling it;
// the worker reads reply.json instead of parsing model prose.
export const name = "foolship-reply";
export const inject = ["tools"];

export function apply(ctx, config) {
  const target = String((config && config.path) || "");
  ctx.tools.register({
    name: "foolship_reply",
    description:
      "Deliver this turn's answer: the human-facing reply, optional follow-up messages, project files and durable memory notes. Call it once, at the end of the turn.",
    parameters: {
      reply: { type: "string", description: "The human-facing answer." },
      messages: {
        type: "array",
        description: "Distinct follow-ups; never repeat reply.",
        items: {
          type: "object",
          properties: { recipient: { type: "string" }, body: { type: "string" } },
        },
      },
      attachments: {
        type: "array",
        description: "Project-relative file paths to send.",
        items: { type: "object", properties: { path: { type: "string" } } },
      },
      memory_notes: {
        type: "array",
        description: "Durable facts from the public group; empty in a private turn.",
        items: { type: "string" },
      },
      work_proposal: {
        type: "object",
        description: "A complete project-work draft for explicit human confirmation; never starts work by itself.",
        properties: {
          id: { type: "string" }, revision: { type: "integer" }, title: { type: "string" },
          instructions: { type: "string" }, deliverable: { type: "string" }, acceptance: { type: "string" },
          validation: { type: "string" }, agent: { type: "string" }, allowed_files: { type: "string" },
          stage: { type: "string", enum: ["discuss", "implement"] },
          visibility: { type: "string", enum: ["group", "private"] },
          reference_message_ids: { type: "array", items: { type: "integer" } },
        },
      },
      work_follow_up: {
        type: "object",
        description: "Select one unambiguous existing work item for the human's explicit same-scope follow-up.",
        properties: { work_id: { type: "string" } },
      },
      work_delivery: {
        type: "object",
        description: "Completion report for the scheduled project-work execution in this turn.",
        properties: {
          summary: { type: "string" }, links: { type: "array", items: { type: "string" } },
          message_id: { type: ["integer", "null"] },
        },
      },
      request: {
        type: "object",
        description:
          "Ask a human for a permission this turn lacked, e.g. writing outside the project directory.",
        properties: {
          reason: { type: "string", description: "What you need and what it unblocks." },
          scope: { type: "string", description: "full_access" },
        },
      },
    },
    output: {
      schema: {
        type: "object",
        additionalProperties: false,
        properties: { ok: { type: "boolean" } },
      },
      render: () => [{ type: "text", text: "reply delivered" }],
    },
    async execute(args) {
      const { writeFileSync } = await import("node:fs");
      writeFileSync(target, JSON.stringify(args && typeof args === "object" ? args : {}), "utf8");
      return { ok: true };
    },
  });
}
"""


PROJECT_TOOLS_JS = """
function checkToolInput(value, schema, path = "参数") {
  if (schema.type !== "object") {
    return matchesAction(value, schema) ? null : path + " 的值不符合本轮要求";
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) return path + " 必须为对象";
  const keys = Object.keys(value);
  for (const key of keys) {
    if (!Object.hasOwn(schema.properties, key)) return path + " 不接受字段 " + key;
  }
  for (const key of schema.required) {
    if (!Object.hasOwn(value, key)) return "缺少字段 " + path + "." + key;
  }
  if (keys.length < (schema.minProperties || 0)) return path + " 至少需要一个修改字段";
  for (const key of keys) {
    const error = checkToolInput(value[key], schema.properties[key], path + "." + key);
    if (error) return error;
  }
  return null;
}

function registerProjectTools(ctx, target) {
  for (const {name, description, inputSchema, kind} of projectTools) {
    ctx.tools.register({
      name,
      description,
      parameters: inputSchema.properties,
      output: {
        schema: {
          type: "object",
          properties: {
            ok: {type: "boolean"}, id: {type: "string"}, revision: {type: "integer"},
            status: {type: "string"}, action: {type: "string"}, message: {type: "string"},
            error_code: {type: "string"}, current_revision: {type: "integer"},
            delivery_message_id: {type: "integer"},
          },
        },
        render: result => [{type: "text", text: JSON.stringify(result)}],
      },
      async execute(args) {
        const message = checkToolInput(args, inputSchema);
        if (message) return {ok: false, error_code: "invalid_arguments", message};
        const {dirname, join} = await import("node:path");
        const {randomUUID} = await import("node:crypto");
        const {writeFile, rename, readFile, rm} = await import("node:fs/promises");
        const id = randomUUID().replace(/-/g, "");
        const request = join(dirname(target), "pt-request-" + id + ".json");
        const result = join(dirname(target), "pt-result-" + id + ".json");
        const temporary = request + ".tmp";
        try {
          await writeFile(temporary, JSON.stringify({action: {kind, ...args}}), {flag: "wx"});
          await rename(temporary, request);
          for (let attempt = 0; attempt < 180; attempt++) {
            try {
              return JSON.parse(await readFile(result, "utf8"));
            } catch (error) {
              if (error.code !== "ENOENT") throw error;
            }
            await new Promise(resolve => setTimeout(resolve, 250));
          }
          return {ok: false, error_code: "result_unconfirmed", message: "未收到 Hub 回执。可用相同参数重试。"};
        } finally {
          await rm(temporary, {force: true});
          await rm(request, {force: true});
          await rm(result, {force: true});
        }
      },
    });
  }
}
"""


def invoke_project_tool(path, action):
    """Ask the enrolled receiver to submit an action and await its Hub receipt."""
    path = Path(path)
    request_id = uuid.uuid4().hex
    request = path / f"pt-request-{request_id}.json"
    result = path / f"pt-result-{request_id}.json"
    try:
        write_json(request, {"action": action})
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            value = read_json(result, tolerant=True)
            if isinstance(value, dict) and type(value.get("ok")) is bool:
                return value
            if (path / "cancel.json").exists():
                break
            time.sleep(0.25)
        return {"ok": False, "error_code": "result_unconfirmed", "message": "未收到 Hub 回执。可用相同参数重试。"}
    finally:
        request.unlink(missing_ok=True)
        result.unlink(missing_ok=True)


def foolship_reply_js(policy=None, *, project_tools=False, file_delivery=False):
    """Render the DSH tool with the trusted turn policy enforced at execution."""
    if policy is None:
        return FOOLSHIP_REPLY_JS
    schema = json.dumps(final_action_schema(policy, project_tools), ensure_ascii=False, separators=(",", ":"))
    helper = f"""
const projectActionSchema = {schema};
function matchesAction(value, schema) {{
  if (schema.anyOf) return schema.anyOf.some((part) => matchesAction(value, part));
  const types = Array.isArray(schema.type) ? schema.type : [schema.type];
  return types.some((type) => {{
    if (schema.enum && !schema.enum.includes(value)) return false;
    if (type === "null") return value === null;
    if (type === "string") return typeof value === "string" && value.length >= (schema.minLength ?? 0) && value.length <= (schema.maxLength ?? 16000);
    if (type === "integer") return Number.isInteger(value) && value > 0 && value < 2 ** 63;
    if (type === "array") return Array.isArray(value) && value.length <= (schema.maxItems ?? 30) &&
      (!schema.uniqueItems || new Set(value).size === value.length) && value.every((v) => matchesAction(v, schema.items));
    if (type !== "object" || !value || Array.isArray(value) || typeof value !== "object") return false;
    const keys = Object.keys(value); const expected = schema.required || Object.keys(schema.properties || {{}});
    return keys.length === expected.length && expected.every((key) => keys.includes(key) && matchesAction(value[key], schema.properties[key]));
  }});
}}
"""
    rendered = FOOLSHIP_REPLY_JS.replace(
        'export const inject = ["tools"];', 'export const inject = ["tools"];\n' + helper
    )
    start = rendered.index("      work_proposal: {")
    end = rendered.index("      request: {", start)
    rendered = rendered[:start] + rendered[end:]
    rendered = rendered.replace("      request: {", "      project_action: projectActionSchema,\n      request: {")
    rendered = rendered.replace('text: "reply delivered"', 'text: "Reply submitted for Hub validation"')
    rendered = rendered.replace(
        'writeFileSync(target, JSON.stringify(args && typeof args === "object" ? args : {}), "utf8");',
        'writeFileSync(target, JSON.stringify(args), { encoding: "utf8", flag: "wx" });',
    )
    rendered = rendered.replace(
        '      const { writeFileSync } = await import("node:fs");',
        '      if (!args || !Object.prototype.hasOwnProperty.call(args, "project_action") || !matchesAction(args.project_action, projectActionSchema)) throw new Error("project_action is not allowed for this turn");\n'
        '      if (["work_proposal", "work_follow_up", "work_delivery"].some((key) => Object.prototype.hasOwnProperty.call(args, key))) throw new Error("legacy project action is not allowed for this turn");\n'
        '      if (Array.isArray(args.tasks) && args.tasks.length) throw new Error("legacy tasks are not allowed for this turn");\n'
        '      const { writeFileSync } = await import("node:fs");',
    )
    if project_tools:
        from agent_harness.project_actions import PROJECT_TOOLS, tool_specs

        definitions = [
            {**spec, "kind": PROJECT_TOOLS[spec["name"]][0]} for spec in tool_specs(policy, file_delivery=file_delivery)
        ]
        rendered = rendered.replace(
            "  ctx.tools.register({",
            "  registerProjectTools(ctx, target);\n  ctx.tools.register({",
            1,
        )
        rendered = rendered.replace(
            '      const { writeFileSync } = await import("node:fs");',
            '      if (args.project_action?.kind === "deliver" && (!Array.isArray(args.attachments) || !args.attachments.length)) throw new Error("Use project_task_deliver for text or links. Final deliver requires file attachments.");\n'
            '      const { writeFileSync } = await import("node:fs");',
            1,
        )
        rendered += "\nconst projectTools = " + json.dumps(definitions, ensure_ascii=False) + ";\n" + PROJECT_TOOLS_JS
    return rendered


def contract_spec(config):
    """The turn's structured context.

    The receiver submits a wrapper: the stage plus an ``objective`` string that is
    itself the JSON context (identity, task, grants, attachments). Reading the
    wrapper alone silently loses every field the model can see, which is how a
    granted permission never reached the sandbox.
    """
    try:
        spec = json.loads(config.get("prompt") or "{}")
    except ValueError:
        return {}
    if not isinstance(spec, dict):
        return {}
    objective = spec.get("objective")
    if isinstance(objective, str):
        try:
            inner = json.loads(objective)
        except ValueError:
            inner = None
        if isinstance(inner, dict):
            merged = dict(inner)
            merged.update({key: value for key, value in spec.items() if key != "objective"})
            return merged
    return spec


def previous_turn_context(config, thread_id):
    """Only reuse context from a completed turn in this exact receiver session."""
    previous_job = config.get("previous_job")
    if not thread_id or thread_id != config.get("thread_id") or not previous_job:
        return None
    try:
        path = job_dir(previous_job)
        state = read_json(path / "state.json", {}, tolerant=True)
        if not isinstance(state, dict) or state.get("thread_id") != thread_id or state.get("status") != "needs_review":
            return None
        prior = read_json(path / "request.json", {}, tolerant=True)
        if not isinstance(prior, dict) or not prior.get("managed_turn") or prior.get("project") != config["project"]:
            return None
        if not config.get("session_file") or prior.get("session_file") != config["session_file"]:
            return None
        return contract_spec(prior)
    except (OSError, ValueError, TypeError):
        return None


def is_chat_context(context):
    messages = context.get("visible_messages")
    return (
        isinstance(context.get("identity"), dict)
        and isinstance(context.get("task"), dict)
        and isinstance(messages, list)
        and all(isinstance(row, dict) and type(row.get("id")) is int for row in messages)
    )


def managed_prompt(config, thread_id, policy, *, project_tools, file_delivery, inbound=()):
    """Render the model input only. Authorization and file handling use the original contract."""
    context = contract_spec(config)
    chat = is_chat_context(context)
    prior = previous_turn_context(config, thread_id) if chat else None
    if prior and (
        not is_chat_context(prior)
        or prior["identity"] != context["identity"]
        or prior.get("project") != context.get("project")
    ):
        prior = None
    first = not prior if chat else not thread_id
    rules = reply_rules(policy, first, project_tools=project_tools, file_delivery=file_delivery)
    sections = [rules]
    if inbound:
        sections.extend(["ATTACHMENTS FROM THE CONVERSATION (inside the project directory):", *inbound])
    if not prior:
        sections.extend(["ASSIGNED LOCAL PROJECT DIRECTORY:", config["project"]["remote_path"]])
    if config.get("memory"):
        sections.extend(["PROJECT MEMORY:", json.dumps(config["memory"], ensure_ascii=False)])
    if chat:
        messages = context["visible_messages"]
        if prior:
            previous_messages = {row["id"]: row for row in prior.get("visible_messages", [])}
            references = referenced_message_ids(context["task"])
            messages = [row for row in messages if row["id"] in references or previous_messages.get(row["id"]) != row]
            for key in (
                "identity",
                "project",
                "members",
                "project_memory",
                "project_work_drafts",
                "project_work_items",
                "protocol",
            ):
                if key in context and context[key] == prior.get(key):
                    del context[key]
            sections.append(
                "Continuation: only new, changed or referenced messages follow. Current task and grants apply."
            )
        context["visible_messages"] = [
            {
                k: v
                for k, v in row.items()
                if k not in {"project_id", "client_message_id", "sender_client"} and v is not None
            }
            for row in messages
        ]
        # The transport repeats these fields inside the task and its wrapper.
        task = context.get("task") or {}
        for key in ("stage", "allowed_files", "acceptance", "validation", "attachments"):
            if key in task and task[key] == context.get(key):
                context.pop(key, None)
    contract = json.dumps(context, ensure_ascii=False, separators=(",", ":")) if chat else config["prompt"]
    sections.extend(["TASK CONTRACT:", contract])
    return "\n".join(sections)


def project_action_policy(config):
    from agent_harness.project_actions import action_policy

    return action_policy(contract_spec(config))


def materialize_inbound(config, remote):
    """Copy the conversation's attachments into the agent's project directory.

    The bytes live on the hub; a sandboxed agent cannot read them from there, so
    each turn writes the files it was given into ``.foolship/inbound`` and lists
    them in the prompt. Nothing is written when the turn carried no attachments.
    """
    spec = contract_spec(config)
    wanted = [
        item for item in spec.get("attachments") or [] if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]
    ids = [item["id"] for item in wanted][:5]
    if not ids:
        return []
    from agent_harness.pull_worker import api

    answer = api(config, "task-inbound", {"ids": ids})
    from agent_harness.attachments import safe_name

    target = Path(remote) / ".foolship" / "inbound"
    target.mkdir(parents=True, exist_ok=True)
    written = []
    for item in answer.get("files") or []:
        name = safe_name(str(item.get("name") or "attachment"))
        path = target / name
        path.write_bytes(base64.b64decode(str(item.get("base64") or "")))
        written.append(path.relative_to(Path(remote)).as_posix())
    return written


def run(job):
    path = job_dir(job)
    directory = None
    state = read_json(path / "state.json", {"job": job})
    proc = None
    try:
        config = read_json(path / "request.json")
        directory = project_dir(config["project"]["name"])
        kind = executors.kind_for(ROOT, config["project"].get("model"))
        exe = cli_path(ROOT, kind)
        login = codex_login_status(exe, ROOT, kind=kind, network=config["project"].get("network"))
        if not login["logged_in"]:
            raise RuntimeError(login["login_error"])
        kind_hint = executors.kind_for(ROOT, config["project"].get("model"))
        policy = project_action_policy(config)
        from agent_harness.project_actions import tool_specs

        contract = contract_spec(config)
        file_delivery = contract.get("project_file_delivery") is True
        project_tools = (
            contract.get("project_tools") is True
            and not config.get("strict_guard")
            and bool(tool_specs(policy, file_delivery=file_delivery))
        )
        if kind_hint == executors.DSH:
            (path / "foolship_reply.js").write_text(
                foolship_reply_js(policy, project_tools=project_tools, file_delivery=file_delivery), encoding="utf-8"
            )
        command_project = dict(config["project"])
        if command_project.get("local_control"):
            command_project["_stage"] = json.loads(config["prompt"]).get("stage", "discuss")
        # Decide the turn's permissions BEFORE the executor command is built: the
        # DSH patch embeds the sandbox mode, so computing it later silently ran
        # every granted turn as workspace-write.
        granted = contract_spec(config).get("grants") or []
        granted_full = "full_access" in granted
        full_access = not command_project.get("local_control") or granted_full
        command_project["_full_access"] = bool(full_access)
        requested_model, actual_effort = executors.effective_request(ROOT, command_project, kind=kind)
        usage = CliUsage(kind, resumed=bool(config.get("thread_id")))
        # Only Codex can enforce the structured reply with a JSON schema; DSH
        # managed turns rely on the GROUP_RULES prompt contract alone.
        managed = bool(config.get("managed_turn"))
        schema_path = None
        if managed and kind == executors.CODEX:
            schema_path = path / "reply-schema.json"
            write_json(schema_path, reply_schema(policy, project_tools=project_tools))
        command = executors.command(
            ROOT,
            exe,
            command_project,
            config["thread_id"],
            scratch=path,
            managed=managed,
            schema_path=schema_path,
            kind=kind,
            **({"project_tools": True} if project_tools else {}),
        )
        try:
            inbound = materialize_inbound(config, config["project"]["remote_path"])
        except Exception:
            inbound = []
        if managed:
            prompt = managed_prompt(
                config,
                config.get("thread_id"),
                policy,
                project_tools=project_tools,
                file_delivery=file_delivery,
                inbound=inbound,
            )
        else:
            sections = [PEER_RULES]
            if inbound:
                sections.extend(["ATTACHMENTS FROM THE CONVERSATION (inside the project directory):", *inbound])
            sections.extend(
                [
                    "ASSIGNED LOCAL PROJECT DIRECTORY:",
                    config["project"]["remote_path"],
                    "PROJECT MEMORY:",
                    json.dumps(config["memory"], ensure_ascii=False),
                    "TASK CONTRACT:",
                    config["prompt"],
                ]
            )
            prompt = "\n".join(sections)
        (path / "prompt.txt").write_text(prompt, encoding="utf-8")
        env = executor_environment(
            ROOT,
            command_project.get("_stage"),
            full_access=full_access,
            network=command_project.get("network"),
            kind=kind,
        )
        with (path / "stderr.log").open("wb") as error:
            proc = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=error,
                cwd=config["project"]["remote_path"],
                env=env,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                start_new_session=os.name != "nt",
            )

            def feed_prompt():
                """Deliver the prompt through a pipe.

                A redirected file handle does not survive a .cmd/.bat launcher on
                Windows: cmd.exe consumes the handle, so the executor reads an
                empty task. Both shipped launchers are batch files, so the prompt
                is streamed from a thread instead.
                """
                try:
                    with (path / "prompt.txt").open("rb") as source:
                        while True:
                            chunk = source.read(65536)
                            if not chunk:
                                break
                            proc.stdin.write(chunk)
                    proc.stdin.flush()
                except (OSError, ValueError, AttributeError):
                    pass
                finally:
                    try:
                        proc.stdin.close()
                    except (OSError, AttributeError):
                        pass

            state.update(
                status="running",
                started_at=now(),
                codex_pid=proc.pid,
                requested_model=requested_model,
                reasoning_effort=config["project"]["effort"],
                effective_reasoning_effort=actual_effort,
                command=command,
                thread_id=config["thread_id"],
            )
            write_json(path / "state.json", state)
            feeder = threading.Thread(target=feed_prompt, daemon=True)
            feeder.start()
            seen = []
            reports = []
            reader_errors = []

            def consume():
                with (path / "events.jsonl").open("wb") as output:
                    for raw in proc.stdout:
                        output.write(raw)
                        output.flush()
                        for provider_event in events(raw.decode("utf-8", errors="replace")):
                            usage.observe(provider_event)
                            for event in executors.normalize(kind, provider_event):
                                seen.append(event)
                                if event["type"] == "thread.started":
                                    thread = event["thread_id"]
                                    state["thread_id"] = thread
                                    with lock(ROOT / "registry.lock"):
                                        write_json(
                                            directory / config.get("session_file", "session.json"),
                                            {"thread_id": thread, "last_job": job},
                                        )
                                    write_json(path / "state.json", state)
                                if (
                                    event["type"] == "item.completed"
                                    and event.get("item", {}).get("type") == "agent_message"
                                ):
                                    reports.append(unfenced_report(event["item"].get("text", "")))
                                    (path / "report.txt").write_text(reports[-1], encoding="utf-8")

            def consume_guarded():
                try:
                    consume()
                except Exception as exc:
                    reader_errors.append(str(exc))

            reader = threading.Thread(target=consume_guarded, daemon=True)
            reader.start()
            deadline = time.monotonic() + execution_timeout(config)
            cancelled = timed_out = False
            while proc.poll() is None:
                cancelled = (path / "cancel.json").exists()
                timed_out = time.monotonic() >= deadline
                if cancelled or timed_out or reader_errors:
                    if os.name == "nt":
                        subprocess.run(
                            ["taskkill.exe", "/PID", str(proc.pid), "/T", "/F"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=30,
                        )
                    else:
                        import signal

                        os.killpg(proc.pid, signal.SIGKILL)
                    break
                time.sleep(0.3)
            code = proc.wait(timeout=30)
            reader.join(timeout=30)
            feeder.join(timeout=5)
            if not reader.is_alive():
                try:
                    proc.stdout.close()
                except (OSError, AttributeError):
                    pass
            state.update(
                status=outcome(code, seen, cancelled, timed_out),
                exit_code=code,
                finished_at=now(),
                effective_model=effective_model(state.get("thread_id"), state["started_at"], kind=kind),
            )
            if reader.is_alive():
                state.update(status="failed", error="Event reader did not drain")
            if reader_errors:
                state.update(status="failed", error="Event reader failed: " + reader_errors[0])
            if not reader.is_alive() and not reader_errors and (totals := usage.result()):
                state["usage"] = totals
            state["model_verified"] = bool(state.get("effective_model"))
            # The reply tool writes the structured answer itself; prefer it over
            # whatever prose the model also produced.
            reply_file = path / "reply.json"
            if reply_file.is_file():
                try:
                    payload = json.loads(reply_file.read_text(encoding="utf-8"))
                except ValueError:
                    payload = None
                if isinstance(payload, dict) and isinstance(payload.get("reply", ""), str):
                    structured = json.dumps(payload, ensure_ascii=False)
                    (path / "report.txt").write_text(structured, encoding="utf-8")
                    reports.append(structured)
            elif policy is not None and kind == executors.DSH and reports:
                # Only the registered tool is trusted to carry a project action.
                try:
                    fallback = json.loads(reports[-1])
                except (TypeError, ValueError):
                    fallback = None
                if isinstance(fallback, dict) and "project_action" in fallback:
                    fallback.pop("project_action", None)
                    reports[-1] = json.dumps(fallback, ensure_ascii=False)
                    (path / "report.txt").write_text(reports[-1], encoding="utf-8")
            if state["status"] == "needs_review" and not reports:
                state.update(status="failed", error="No report returned")
            if (
                state["status"] == "needs_review"
                and project_tools
                and reports
                and (error := project_delivery_error(reports[-1]))
            ):
                state.update(status="failed", error=error)
            if state.get("effective_model") and state["effective_model"] != state["requested_model"]:
                state.update(
                    status="failed",
                    error="Effective model differs from requested model",
                )
            violation = _check_strict_guard(config.get("strict_guard"))
            if violation:
                state.update(status="failed", error=violation)
            if kind == executors.DSH and state["status"] == "failed":
                # DSH reports its own failure code on stderr; replace the generic
                # "no report" outcome with something the machine owner can act on.
                hint = executors.explain_failure(tail(path / "stderr.log", 8000))
                if hint:
                    state["provider_error"] = state.get("error")
                    state["error"] = hint
    except Exception as exc:
        if proc and proc.poll() is None:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill.exe", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True,
                )
            else:
                import signal

                os.killpg(proc.pid, signal.SIGKILL)
        state.update(status="failed", error=str(exc), finished_at=now())
    finally:
        with lock(ROOT / "registry.lock"):
            # A cancel accepted before the final state commit wins, even if CLI just exited.
            cancel = read_json(path / "cancel.json")
            if cancel:
                state.update(status="cancelled", cancel_requested_at=cancel["requested_at"])
            write_json(path / "state.json", state)
            if directory:
                active = read_json(directory / "active.json")
                if active and active["job"] == job:
                    write_json(directory / "active.json", None)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "run":
        job = identifier(sys.argv[2])
        with (job_dir(job) / "supervisor.log").open("a", encoding="utf-8", buffering=1) as log:
            sys.stdout = sys.stderr = log
            run(job)
    else:
        try:
            request = json.loads(sys.stdin.buffer.read().decode("utf-8"))
            result = rpc(request)
            print(json.dumps({"ok": True, "result": result}, ensure_ascii=True))
        except Exception as exc:
            print(json.dumps({"ok": False, "error": str(exc)}))
            sys.exit(1)
