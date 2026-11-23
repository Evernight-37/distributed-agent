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
    tail,
    validate_project,
    write_json,
)
from agent_harness.node_api import NodeExecutionAPI, NodeJobActions, NodeLocalActions, NodeProjectActions

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


def codex_login_status(exe, root=None, kind=None):
    """Check the executor that will run this turn, without reading credentials.

    The historical name is kept because the worker, the desktop settings API and
    the Linux receiver all call it through this module.
    """
    root = ROOT if root is None else Path(root)
    return executors.login(root, exe, kind)


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
            job = identifier(request["job"])
            path = job_dir(job)
            path.mkdir(parents=True, exist_ok=False)
            # The agent's model decides which local CLI runs this turn. A model the
            # table does not list falls back to the machine's own executor, so an
            # existing agent keeps working before its model is added to the table.
            kind = executors.kind_for(ROOT, project.get("model"))
            channel = request.get("session_channel")
            session_file = "session-" + kind + ".json"
            if channel:
                identifier(channel)
                session_file = "channels/" + channel + "-" + kind + ".json"
            elif (ROOT / "projects").joinpath(project["name"]).joinpath(session_file).is_file() is False:
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
If a foolship_reply tool is available, call it once with {reply, messages, attachments, memory_notes} instead of writing JSON. Otherwise reply with one JSON object: {"reply": str, "messages": [{recipient, body}], "attachments": [{path}], "tasks": [...], "executions": [...], "memory_notes": [str], "done": bool}.
- reply is the human-facing answer, without private reasoning or progress logs. messages are for distinct follow-ups only and never repeat reply.
- attachments are project-relative paths: existing ordinary files inside the project, at most five, each at most 5 MB; never hidden files, credentials or paths outside it.
- memory_notes only for durable facts from the public group; empty in a private turn.
Use the tools your executor gives you; the stage names the humans' intent, not a limit. You may spawn your own sub-agents and remain responsible for the combined result. If something you need is denied, put request:{"reason": "..."} in the reply so a human can approve it with /allow.
Never reveal a human's private message outside its conversation. Do not read credentials or contact outside parties unless the task says so.
"""

# Every resumed turn still needs the reply contract, even though the full rules
# are only sent once per thread.
REPLY_REMINDER = 'Reply with one JSON object: {"reply": "...", "messages": [], "attachments": [], "memory_notes": []}.'


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


def effective_model(thread_id, since=None):
    """The model the executor actually used, when it records one."""
    return executors.effective_model(executors.selected(ROOT), thread_id, since)


def reply_schema():
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

    return obj(
        {
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
            # A request the human answers with /allow or /deny.
            "request": {
                "type": "object",
                "properties": {"reason": {"type": "string"}, "scope": {"type": "string"}},
            },
        }
    )


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
        login = codex_login_status(exe, ROOT, kind=kind)
        if not login["logged_in"]:
            raise RuntimeError(login["login_error"])
        kind_hint = executors.kind_for(ROOT, config["project"].get("model"))
        if kind_hint == executors.DSH:
            (path / "foolship_reply.js").write_text(FOOLSHIP_REPLY_JS, encoding="utf-8")
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
        # Only Codex can enforce the structured reply with a JSON schema; DSH
        # managed turns rely on the GROUP_RULES prompt contract alone.
        managed = bool(config.get("managed_turn"))
        schema_path = None
        if managed and kind == executors.CODEX:
            schema_path = path / "reply-schema.json"
            write_json(schema_path, reply_schema())
        command = executors.command(
            ROOT,
            exe,
            command_project,
            config["thread_id"],
            scratch=path,
            managed=managed,
            schema_path=schema_path,
            kind=kind,
        )
        rules = GROUP_RULES if managed else PEER_RULES
        # The executor resumes a thread, so its history already holds the rules.
        # Re-sending them every turn repeats ~1k tokens and dilutes attention;
        # only the reply contract has to stay in front of the model each time.
        first_turn = not config.get("thread_id")
        sections = [rules if first_turn else REPLY_REMINDER]
        try:
            inbound = materialize_inbound(config, config["project"]["remote_path"])
        except Exception:
            inbound = []
        if inbound:
            sections += [
                "ATTACHMENTS FROM THE CONVERSATION (inside the project directory):",
                "\n".join(inbound),
            ]
        sections += [
            "ASSIGNED LOCAL PROJECT DIRECTORY:",
            config["project"]["remote_path"],
            "PROJECT MEMORY:",
            json.dumps(config["memory"], ensure_ascii=False),
            "TASK CONTRACT:",
            config["prompt"],
        ]
        prompt = "\n".join(sections)
        (path / "prompt.txt").write_text(prompt, encoding="utf-8")
        # A session logs the sandbox mode it started with, and that log outranks
        # the deployment default on every later turn. Resuming a session created
        # under workspace-write therefore ignores a newly granted full_access, so
        # a mode change starts a fresh session instead.
        required_mode = "danger-full-access" if full_access else "workspace-write"
        marker = directory / "sandbox-mode.txt" if directory is not None else None
        previous_mode = marker.read_text(encoding="utf-8").strip() if marker and marker.is_file() else ""
        if previous_mode and previous_mode != required_mode and config.get("thread_id"):
            config["thread_id"] = None
        if marker is not None:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(required_mode, encoding="utf-8")
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
                requested_model=executors.requested_model(ROOT, config["project"]["model"], kind=kind),
                reasoning_effort=config["project"]["effort"],
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
                effective_model=effective_model(state.get("thread_id"), state["started_at"]),
            )
            if reader.is_alive():
                state.update(status="failed", error="Event reader did not drain")
            if reader_errors:
                state.update(status="failed", error="Event reader failed: " + reader_errors[0])
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
            if state["status"] == "needs_review" and not reports:
                state.update(status="failed", error="No report returned")
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
