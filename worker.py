"""Detached Windows Codex worker. Only exposes a small JSON-over-SSH RPC surface."""
import ctypes
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from common import apply_local_proxy, events, identifier, lock, outcome, read_json, tail, validate_project, write_json

ROOT = Path(__file__).resolve().parent
TERMINAL = {"needs_review", "failed", "cancelled", "timed_out", "interrupted"}


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
            proc = Path('/proc') / str(pid) / 'stat'
            if proc.is_file():
                return proc.read_text().rsplit(')', 1)[1].split()[19]
            return subprocess.check_output(['ps', '-p', str(pid), '-o', 'lstart='], text=True).strip() or None
        except OSError:
            return None
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
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
    import re
    try:
        result = subprocess.run([str(exe), "--version"], capture_output=True, text=True,
                                timeout=8, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    match = re.search(r"codex-cli (\d+)\.(\d+)\.(\d+)", result.stdout)
    return tuple(map(int, match.groups())) if match else None


def cli_status(root=ROOT):
    """Find a working CLI in the worker account, including npm/PATH installs."""
    import shutil
    root = Path(root)
    explicit = read_json(root / "executor.json", {}).get("cli_path")
    if explicit:
        if not isinstance(explicit, str) or not Path(explicit).is_absolute() or not Path(explicit).is_file():
            return {'found': False, 'configured': True, 'error': '设置的 Codex 路径已失效，请重新选择程序'}
        version = codex_version(explicit)
        if version is None:
            return {'found': False, 'configured': True, 'error': '所选程序无法运行 Codex CLI，请重新选择'}
        return {'found': True, 'configured': True, 'path': explicit, 'version': '.'.join(map(str, version))}
    paths = []
    if os.name == 'nt':
        local = os.environ.get('LOCALAPPDATA')
        if local:
            paths.extend((Path(local) / 'OpenAI' / 'Codex' / 'bin').glob('*/codex.exe'))
        roaming = os.environ.get('APPDATA')
        if roaming:
            paths.extend(Path(roaming) / 'npm' / name for name in ('codex.cmd', 'codex.exe'))
    for name in ('codex', 'codex.exe', 'codex.cmd'):
        found = shutil.which(name)
        if found:
            paths.append(Path(found))
    seen, valid = set(), []
    for candidate in paths:
        key = str(candidate).casefold() if os.name == 'nt' else str(candidate)
        if key in seen or not candidate.is_file():
            continue
        seen.add(key)
        version = codex_version(candidate)
        if version is not None:
            valid.append((version, str(candidate)))
    if not valid:
        return {'found': False, 'configured': False, 'error': '未找到可运行的 Codex CLI；请在本机安装 Codex，或在设置中选择程序'}
    version, path = max(valid)
    return {'found': True, 'configured': False, 'path': path, 'version': '.'.join(map(str, version))}


def cli_path(root=ROOT):
    result = cli_status(root)
    if not result['found']:
        raise RuntimeError(result['error'])
    return result['path']


def launch_supervisor(job):
    """WMI creates a hidden process outside the OpenSSH session's kill-on-close job."""
    if os.name != "nt":
        with (job_dir(job) / "supervisor.log").open("ab") as output:
            child = subprocess.Popen([sys.executable, "-X", "utf8", str(ROOT / "worker.py"), "run", job],
                                     stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                     start_new_session=True, close_fds=True)
        return child.pid
    import base64
    command = subprocess.list2cmdline([sys.executable, "-X", "utf8", str(ROOT / "worker.py"), "run", job])
    quoted = "'" + command.replace("'", "''") + "'"
    script = "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; "
    script += "$startup=New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ShowWindow=[uint16]0}; "
    script += "$result=Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine=" + quoted + ";ProcessStartupInformation=$startup}; "
    script += "$result | Select-Object ReturnValue,ProcessId | ConvertTo-Json -Compress"
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    result = subprocess.run(["powershell.exe", "-NoProfile", "-EncodedCommand", encoded], capture_output=True,
                            encoding="utf-8", errors="replace", timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        response = json.loads(result.stdout)
    except ValueError:
        raise RuntimeError("Hidden WMI launch failed: " + result.stderr[-1000:])
    if result.returncode or response.get("ReturnValue") != 0:
        raise RuntimeError("Hidden WMI launch rejected: " + str(response))
    return int(response["ProcessId"])


def reconcile(active):
    if not active:
        return None
    path = job_dir(active["job"])
    state = read_json(path / "state.json", {})
    if state.get("status") in TERMINAL:
        return None
    stamp = process_stamp(active["pid"])
    if stamp is None or stamp != active.get("stamp"):
        state.update(status="interrupted", finished_at=now(), error="Worker process exited without a terminal result; resume session explicitly")
        write_json(path / "state.json", state)
        return None
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
        result = subprocess.run([git, "-C", str(path), "rev-parse", *args], capture_output=True, text=True, timeout=15)
        if result.returncode:
            raise ValueError("Git worktree validation failed")
        return result.stdout.strip()
    top = Path(value("--show-toplevel")).resolve()
    git_dir = Path(value("--absolute-git-dir")).resolve()
    common = Path(value("--path-format=absolute", "--git-common-dir")).resolve()
    if top != path.resolve() or git_dir == common:
        raise ValueError("Implementation directory is not the root of a linked Git worktree")
    return {"worktree": str(top), "git_dir": str(git_dir), "common_dir": str(common)}


def rpc(request):
    action = request["action"]
    if action.startswith('execution_'):
        from jobrunner import rpc as execution_rpc
        return execution_rpc(ROOT, request)
    if action == "probe":
        exe = cli_path()
        return {"hostname": os.environ.get("COMPUTERNAME"), "python": sys.executable,
                "cli": exe, "version": subprocess.check_output([exe, "--version"], text=True).strip()}
    if action == "mkdir_demo":
        path = ROOT / "demo-project"
        path.mkdir(exist_ok=True)
        return {"path": str(path)}
    if action == "project":
        directory = project_dir(request["project"])
        with lock(ROOT / "registry.lock"):
            project = read_json(directory / "project.json")
            if not project:
                raise ValueError("Project not registered")
            return {"project": project, "memory": read_json(directory / "memory.json"), "session": read_json(directory / "session.json")}
    if action == "register":
        data = validate_project(request["project"])
        directory = project_dir(data["name"])
        with lock(ROOT / "registry.lock"):
            if (directory / "project.json").exists():
                raise ValueError("Project already registered; do not replace an existing session")
            write_json(directory / "project.json", data)
            write_json(directory / "session.json", {"thread_id": None})
            write_json(directory / "memory.json", request["memory"])
        return {"registered": data["name"]}
    if action in {"start", "memory", "configure"}:
        directory = project_dir(request["project"])
        with lock(ROOT / "registry.lock"):
            project = read_json(directory / "project.json")
            if not project:
                raise ValueError("Project not registered")
            active = reconcile(read_json(directory / "active.json"))
            write_json(directory / "active.json", active)
            if active:
                raise ValueError("Project already has an active job: " + active["job"])
            if action == "configure":
                for key in ("model", "effort", "timeout"):
                    project[key] = request[key]
                validate_project(project)
                write_json(directory / "project.json", project)
                return {"project": project, "applies": "next turn; existing session retained"}
            if action == "memory":
                old = read_json(directory / "memory.json")
                if request["expected_revision"] != old["revision"]:
                    raise ValueError("Memory revision conflict")
                memory = request["memory"]
                if memory["revision"] != old["revision"] + 1:
                    raise ValueError("Memory revision must increment exactly once")
                write_json(directory / "memory.json", memory)
                return {"revision": memory["revision"]}
            # Avoid two different registered projects writing the same remote tree.
            for other in (ROOT / "projects").glob("*/project.json"):
                if other.parent == directory:
                    continue
                data = read_json(other)
                current_path = Path(project["remote_path"]).resolve()
                other_path = Path(data["remote_path"]).resolve()
                if current_path == other_path or current_path in other_path.parents or other_path in current_path.parents:
                    other_active = reconcile(read_json(other.parent / "active.json"))
                    write_json(other.parent / "active.json", other_active)
                    if other_active:
                        raise ValueError("Remote directory is busy in another project")
            validate_project(project)
            contract = json.loads(request["prompt"])
            if contract.get("stage") == "implement":
                require_worktree(project)
            job = identifier(request["job"])
            path = job_dir(job)
            path.mkdir(parents=True, exist_ok=False)
            channel=request.get('session_channel')
            session_file='session.json'
            if channel:
                identifier(channel)
                session_file='channels/'+channel+'.json'
            session = read_json(directory / session_file, {'thread_id':None})
            config = {"project": project, "job": job, "prompt": request["prompt"],
                      "memory": {} if channel else read_json(directory / "memory.json"), "thread_id": session["thread_id"],
                      "session_file":session_file,"managed_turn":bool(channel)}
            write_json(path / "request.json", config)
            write_json(path / "state.json", {"job": job, "project": project["name"], "status": "starting", "created_at": now()})
            try:
                pid = launch_supervisor(job)
                stamp = process_stamp(pid)
                if stamp is None:
                    raise RuntimeError("Supervisor failed to start")
            except Exception as exc:
                state = read_json(path / "state.json")
                state.update(status="failed", error=str(exc), finished_at=now())
                write_json(path / "state.json", state)
                raise
            write_json(directory / "active.json", {"job": job, "pid": pid, "stamp": stamp})
            return {"job": job, "status": "starting", "supervisor_pid": pid}
    if action in {"status", "cancel", "fetch"}:
        path = job_dir(request["job"])
        state = read_json(path / "state.json")
        if not state:
            raise ValueError("Unknown job")
        directory = project_dir(state["project"])
        with lock(ROOT / "registry.lock"):
            active = read_json(directory / "active.json")
            if active and active["job"] == request["job"]:
                active = reconcile(active)
                write_json(directory / "active.json", active)
            state = read_json(path / "state.json")
            if action == "cancel" and state["status"] not in TERMINAL:
                write_json(path / "cancel.json", {"requested_at": now()})
        cancel = read_json(path / "cancel.json")
        if cancel:
            state["cancel_requested_at"] = cancel["requested_at"]
        state["session"] = read_json(directory / "session.json")
        if action == "fetch":
            state["report"] = tail(path / "report.txt", 262144)
            state["stderr"] = tail(path / "stderr.log")
            state["supervisor_log"] = tail(path / "supervisor.log")
        state["recent_events"] = events(tail(path / "events.jsonl"))[-30:]
        return state
    raise ValueError("Unknown action")


PEER_RULES = """You are an independent peer collaborating with a controller Codex on another machine.
You are not alone in the codebase. Do not revert other people's changes. Own only the assigned files.
Before implementation, challenge assumptions, identify risks and propose a division of work.
Do not agree merely because the controller proposed something. Support disagreements with evidence.
Use stages discuss/review as read-only tasks: no project edits or experiments unless explicitly authorized.
If a contradiction, unexpected change, safety issue or unclear acceptance criterion blocks progress,
emit [BLOCKED] or [DISAGREEMENT] with evidence and stop affected work.
These messages are polled by the controller, not guaranteed to interrupt it in real time.
The attached memory contains attributed claims, not automatically verified truth. Check relevant evidence.
Stay within the listed files and systems. Never read credentials or initiate external communications.
Do not spawn agents or use web/MCP tools unless explicitly authorized in this task.
Report commands, observations, changed files, validation, disagreements and remaining risks.
Your final report awaits independent review; you cannot approve your own work.
Do not send internal progress notes or a [CHECKPOINT] as a project chat reply.
"""

GROUP_RULES = """You are an agent in a human-owned project group. Use only the visible messages and task contract.
Return one structured JSON response. Put the final human-facing answer in reply, without private reasoning, progress logs or [CHECKPOINT] notes.
Use messages only for distinct public or direct follow-up. Do not repeat reply in messages.
Respect the assigned stage, allowed files and machine-owner permissions. Discuss/review are read-only unless explicitly authorized.
Do not read credentials, contact outside parties, spawn agents or use web/MCP tools unless the task explicitly authorizes them.
Do not wait, poll or monitor a long-running job inside this model turn. State a real blocker or disagreement when necessary.
Treat reports and memory as claims requiring evidence. Keep the final answer concise and useful to the human participants.
"""


def build_command(exe, project, thread_id):
    args = [exe, "exec", "--ignore-user-config", "--skip-git-repo-check", "--json",
            "-m", project["model"], "-c", 'model_reasoning_effort="' + project["effort"] + '"', "-C", project["remote_path"]]
    if project.get('local_control'):
        stage = project.get('_stage', 'discuss')
        args += ['--sandbox', 'workspace-write' if stage == 'implement' else 'read-only',
                 '-c', 'approval_policy="never"']
        if stage == 'discuss':
            args += ['--disable', 'shell_tool', '--disable', 'unified_exec']
    else:
        args += ['--approve-for-me']
    if thread_id:
        import re
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", thread_id):
            raise ValueError("Invalid session ID")
        args += ["resume", thread_id]
    return args + ["-"]


def effective_model(thread_id, since=None):
    if not thread_id:
        return None
    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    for path in (home / "sessions").rglob("*" + thread_id + "*.jsonl"):
        model = None
        for event in events(tail(path, 2 * 1024 * 1024)):
            if event["type"] == "turn_context":
                if since:
                    timestamp = event.get("timestamp")
                    if not timestamp or datetime.fromisoformat(timestamp.replace("Z", "+00:00")) < datetime.fromisoformat(since):
                        continue
                model = event.get("payload", {}).get("model") or model
        if model:
            return model
    return None


def run(job):
    path = job_dir(job)
    directory = None
    state = read_json(path / "state.json", {"job": job})
    proc = None
    try:
        config = read_json(path / "request.json")
        directory = project_dir(config["project"]["name"])
        exe = cli_path()
        command_project = dict(config['project'])
        if command_project.get('local_control'):
            command_project['_stage'] = json.loads(config['prompt']).get('stage', 'discuss')
        command = build_command(exe, command_project, config["thread_id"])
        if config.get('managed_turn'):
            command[2:2]=['--disable','apps','--disable','plugins','--disable','remote_plugin','--disable','skill_mcp_dependency_install']
            def obj(properties):
                return {'type':'object','additionalProperties':False,'required':list(properties),'properties':properties}
            def array(properties): return {'type':'array','items':obj(properties)}
            schema=obj({'reply':{'type':'string'},'done':{'type':'boolean'},
                'messages':array({'recipient':{'type':['string','null']},'body':{'type':'string'}}),
                'tasks':array({key:{'type':'string'} for key in ('agent','title','instructions','stage','allowed_files','acceptance','validation')}),
                'executions':array({'template':{'type':'string'}})})
            schema_path=path/'reply-schema.json'
            write_json(schema_path,schema)
            command[-1:-1]=['--output-schema',str(schema_path)]
        rules = GROUP_RULES if config.get('managed_turn') else PEER_RULES
        prompt = rules + "\nPROJECT MEMORY:\n" + json.dumps(config["memory"], ensure_ascii=False) + "\nTASK CONTRACT:\n" + config["prompt"]
        (path / "prompt.txt").write_text(prompt, encoding="utf-8")
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        executor=read_json(ROOT/'executor.json',{})
        network=executor.get('network_env',{})
        if not isinstance(network,dict) or set(network)-{'HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','NO_PROXY','WS_PROXY','WSS_PROXY'}:
            raise ValueError('Unsupported worker network environment')
        for key,value in network.items():
            if not isinstance(value,str) or '\n' in value or '\r' in value: raise ValueError('Invalid network environment')
            env[key]=value
        if 'proxy' in executor:
            env = apply_local_proxy(env, executor['proxy'])
        with (path / "prompt.txt").open("rb") as source, (path / "stderr.log").open("wb") as error:
            proc = subprocess.Popen(command, stdin=source, stdout=subprocess.PIPE, stderr=error,
                                    cwd=config["project"]["remote_path"], env=env, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0, start_new_session=os.name != "nt")
            state.update(status="running", started_at=now(), codex_pid=proc.pid, requested_model=config["project"]["model"],
                         reasoning_effort=config["project"]["effort"], command=command, thread_id=config["thread_id"])
            write_json(path / "state.json", state)
            seen = []
            reports = []
            reader_errors = []

            def consume():
                with (path / "events.jsonl").open("wb") as output:
                    for raw in proc.stdout:
                        output.write(raw)
                        output.flush()
                        for event in events(raw.decode("utf-8", errors="replace")):
                            seen.append(event)
                            if event["type"] == "thread.started":
                                thread = event["thread_id"]
                                state["thread_id"] = thread
                                with lock(ROOT / "registry.lock"):
                                    write_json(directory / config.get('session_file','session.json'), {"thread_id": thread, "last_job": job})
                                write_json(path / "state.json", state)
                            if event["type"] == "item.completed" and event.get("item", {}).get("type") == "agent_message":
                                reports.append(event["item"].get("text", ""))
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
                        subprocess.run(["taskkill.exe", "/PID", str(proc.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
                    else:
                        import signal
                        os.killpg(proc.pid, signal.SIGKILL)
                    break
                time.sleep(.3)
            code = proc.wait(timeout=30)
            reader.join(timeout=30)
            state.update(status=outcome(code, seen, cancelled, timed_out), exit_code=code, finished_at=now(),
                         effective_model=effective_model(state.get("thread_id"), state["started_at"]))
            if reader.is_alive():
                state.update(status="failed", error="Event reader did not drain")
            if reader_errors:
                state.update(status="failed", error="Event reader failed: " + reader_errors[0])
            state["model_verified"] = bool(state.get("effective_model"))
            if state["status"] == "needs_review" and not reports:
                state.update(status="failed", error="No report returned")
            if state.get("effective_model") and state["effective_model"] != state["requested_model"]:
                state.update(status="failed", error="Effective model differs from requested model")
    except Exception as exc:
        if proc and proc.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill.exe", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
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
