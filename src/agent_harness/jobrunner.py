"""Independent, non-LLM background execution for registered projects.

Jobs are selected from administrator-approved templates.  The runner never
accepts an arbitrary command from an RPC caller and does not involve Codex.
"""

import base64
import ctypes
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from agent_harness.common import identifier, lock, read_json, tail, write_json

TERMINAL = {"completed", "failed", "cancelled", "timed_out", "interrupted"}


def now():
    return datetime.now(timezone.utc).isoformat()


def _project(root, name):
    return Path(root) / "projects" / identifier(name)


def _execution(root, name):
    return Path(root) / "executions" / identifier(name)


def _stamp(pid):
    if sys.platform == "darwin":
        try:
            os.kill(pid, 0)
            result = subprocess.run(
                ["/bin/ps", "-p", str(pid), "-o", "lstart="],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.stdout.strip() if result.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            return None
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            stat = (Path("/proc") / str(pid) / "stat").read_text(encoding="ascii")
            return stat.rsplit(")", 1)[1].split()[19]
        except OSError:
            return None
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return None
    try:
        code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:
            return None
        stamps = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(x) for x in stamps)):
            return None
        return str((stamps[0].dwHighDateTime << 32) | stamps[0].dwLowDateTime)
    finally:
        kernel.CloseHandle(handle)


def _validate_templates(project, templates):
    if not isinstance(templates, dict) or not templates:
        raise ValueError("templates must be a non-empty object")
    cwd = Path(project["remote_path"]).resolve()
    if not cwd.is_dir():
        raise ValueError("Registered project directory does not exist")
    checked = {}
    for name, value in templates.items():
        identifier(name)
        if not isinstance(value, dict) or set(value) != {"argv"}:
            raise ValueError("Template must contain only argv")
        argv = value["argv"]
        if (
            not isinstance(argv, list)
            or not 1 <= len(argv) <= 64
            or any(not isinstance(x, str) or len(x) > 8192 for x in argv)
        ):
            raise ValueError("argv must contain 1-64 bounded strings")
        exe = Path(argv[0])
        if not exe.is_absolute() or not exe.is_file():
            raise ValueError("Template executable must be an existing absolute file")
        checked[name] = {"argv": list(argv)}
    return checked


def _launch(root, execution):
    if os.name != "nt":
        out = (execution / "supervisor.log").open("ab")
        try:
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-X",
                    "utf8",
                    str(Path(__file__).resolve()),
                    "run",
                    str(root),
                    execution.name,
                ],
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=out,
                start_new_session=True,
                close_fds=True,
            )
        finally:
            out.close()
        return child.pid
    command = subprocess.list2cmdline(
        [
            sys.executable,
            "-X",
            "utf8",
            str(Path(__file__).resolve()),
            "run",
            str(root),
            execution.name,
        ]
    )
    script = (
        "$ErrorActionPreference='Stop';$s=New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ShowWindow=[uint16]0};$r=Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine='"
        + command.replace("'", "''")
        + "';ProcessStartupInformation=$s};$r|ConvertTo-Json -Compress"
    )
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
        data = json.loads(result.stdout)
    except ValueError:
        raise RuntimeError("Hidden supervisor launch failed: " + result.stderr[-1000:])
    if result.returncode or data.get("ReturnValue") != 0:
        raise RuntimeError("Hidden supervisor launch rejected")
    return int(data["ProcessId"])


def _kill(pid):
    if os.name == "nt":
        subprocess.run(
            ["taskkill.exe", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            timeout=30,
        )
    else:
        os.killpg(pid, signal.SIGKILL)


def _reconcile(root, state):
    if state.get("status") in TERMINAL:
        return state
    pid = state.get("supervisor_pid") or state.get("pid")
    if not pid:
        # The launcher writes the PID immediately after creating the process;
        # tolerate that handshake window instead of declaring a fresh job dead.
        created = state.get("created_at")
        if created:
            try:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(created)).total_seconds()
                if age <= 15:
                    return state
            except ValueError:
                pass
        state.update(
            status="interrupted",
            finished_at=now(),
            error="Supervisor launch handshake failed",
        )
        write_json(_execution(root, state["execution"]) / "state.json", state)
        return state
    expected = state.get("supervisor_stamp") or state.get("stamp")
    if expected is None or _stamp(pid) != expected:
        state.update(status="interrupted", finished_at=now(), error="Supervisor exited")
        write_json(_execution(root, state["execution"]) / "state.json", state)
    return state


def _run(root, execution_id):
    path = _execution(root, execution_id)
    request = read_json(path / "request.json")
    state = read_json(path / "state.json", {"execution": execution_id})
    proc = None
    try:
        with (
            (path / "stdout.log").open("ab") as output,
            (path / "stderr.log").open("ab") as error,
        ):
            proc = subprocess.Popen(
                request["argv"],
                cwd=request["cwd"],
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=error,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                start_new_session=os.name != "nt",
            )
        with lock(Path(root) / "registry.lock"):
            state = read_json(path / "state.json", state)
            state.update(status="running", pid=proc.pid, stamp=_stamp(proc.pid), started_at=now())
            write_json(path / "state.json", state)
        deadline = time.monotonic() + request["timeout"]
        while proc.poll() is None:
            if (path / "cancel.json").exists():
                _kill(proc.pid)
                state["status"] = "cancelled"
                break
            if time.monotonic() >= deadline:
                _kill(proc.pid)
                state["status"] = "timed_out"
                break
            time.sleep(0.2)
        code = proc.wait(timeout=30)
        state["exit_code"] = code
        if state.get("status") == "running":
            state["status"] = "completed" if code == 0 else "failed"
    except Exception as exc:
        if proc and proc.poll() is None:
            _kill(proc.pid)
        state.update(status="failed", error=str(exc))
    state["finished_at"] = now()
    with lock(Path(root) / "registry.lock"):
        # A cancellation accepted while the process was active wins the final
        # state commit, even if the child exited at the same instant.
        cancel = read_json(path / "cancel.json")
        if cancel:
            state["status"] = "cancelled"
            state["cancel_requested_at"] = cancel.get("requested_at")
        write_json(path / "state.json", state)


def rpc(root, request):
    root = Path(root)
    action = request.get("action")
    if action == "execution_templates":
        project = read_json(_project(root, request["project"]) / "project.json")
        if not project:
            raise ValueError("Unknown project")
        checked = _validate_templates(project, request["templates"])
        with lock(root / "registry.lock"):
            active = [read_json(p / "state.json", {}) for p in (root / "executions").glob("*/")]
            if any(x.get("project") == project["name"] and x.get("status") not in TERMINAL for x in active):
                raise ValueError("Project has active executions")
            write_json(_project(root, project["name"]) / "execution_templates.json", checked)
        return {"project": project["name"], "templates": checked}
    if action == "execution_start":
        project = read_json(_project(root, request["project"]) / "project.json")
        if not project:
            raise ValueError("Unknown project")
        execution_id = identifier(request["execution"])
        path = _execution(root, execution_id)
        with lock(root / "registry.lock"):
            if path.exists():
                raise ValueError("Execution already exists")
            templates = read_json(_project(root, project["name"]) / "execution_templates.json", {})
            template = templates.get(request.get("template"))
            if not template:
                raise ValueError("Unknown execution template")
            request_data = {
                "execution": execution_id,
                "project": project["name"],
                "argv": template["argv"],
                "cwd": str(Path(project["remote_path"]).resolve()),
                "timeout": project.get("timeout", 1800),
            }
            path.mkdir(parents=True)
            write_json(path / "request.json", request_data)
            write_json(
                path / "state.json",
                {
                    "execution": execution_id,
                    "project": project["name"],
                    "status": "starting",
                    "created_at": now(),
                },
            )
            try:
                pid = _launch(root, path)
            except Exception as exc:
                write_json(
                    path / "state.json",
                    {
                        "execution": execution_id,
                        "project": project["name"],
                        "status": "failed",
                        "error": str(exc),
                        "finished_at": now(),
                    },
                )
                raise
            state = read_json(path / "state.json")
            stamp = _stamp(pid)
            state["supervisor_pid"] = pid
            state["supervisor_stamp"] = stamp
            if stamp is None:
                state.update(
                    status="failed",
                    error="Supervisor did not survive launch",
                    finished_at=now(),
                )
                write_json(path / "state.json", state)
                raise RuntimeError(state["error"])
            write_json(path / "state.json", state)
        return {"execution": execution_id, "status": "starting"}
    if action in {"execution_status", "execution_cancel"}:
        execution_id = identifier(request["execution"])
        path = _execution(root, execution_id)
        with lock(root / "registry.lock"):
            state = read_json(path / "state.json")
            if not state:
                raise ValueError("Unknown execution")
            if action == "execution_cancel" and state.get("status") not in TERMINAL:
                cancel = {"requested_at": now()}
                write_json(path / "cancel.json", cancel)
                state["cancel_requested_at"] = cancel["requested_at"]
            state = _reconcile(root, state)
        state["stdout_tail"] = tail(path / "stdout.log")
        state["stderr_tail"] = tail(path / "stderr.log")
        return state
    raise ValueError("Unknown execution action")


if __name__ == "__main__" and len(sys.argv) == 4 and sys.argv[1] == "run":
    _run(Path(sys.argv[2]), identifier(sys.argv[3]))
