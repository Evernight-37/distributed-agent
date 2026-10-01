"""Personal Windows-to-Windows Codex harness. Python standard library only."""
import argparse
import base64
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath

from common import identifier, lock, read_json, write_json

SOURCE = Path(__file__).resolve().parent
DEFAULT_STATE = Path(os.environ.get("AGENT_HARNESS_STATE", str(SOURCE.parent / "agent-harness-data")))
TERMINAL = {"needs_review", "failed", "cancelled", "timed_out", "interrupted"}


class RemoteRejected(RuntimeError):
    pass


def ps_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


class Harness:
    def __init__(self, state, node=None):
        self.root = Path(state).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.node = node

    def config(self):
        if self.node:
            config = read_json(self.root / "nodes" / (identifier(self.node) + ".json"))
            if not config:
                raise ValueError("Unknown node")
            return config
        config = read_json(self.root / "connection.json")
        if not config:
            raise ValueError("Run init first")
        return config

    def transport(self, script, payload, config=None, timeout=45):
        config = config or self.config()
        import shutil
        args = [shutil.which("ssh") or "ssh", "-i", config["identity_file"], "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=yes", "-o", "UserKnownHostsFile=" + config["known_hosts_file"],
                "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2",
                "-p", str(config.get("port", 22)), config["user"] + "@" + config["host"]]
        if config.get("platform", "windows") == "windows":
            encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
            args.append("powershell.exe -NoProfile -EncodedCommand " + encoded)
        else:
            args.append(script)
        try:
            result = subprocess.run(args, input=json.dumps(payload, ensure_ascii=False) + "\n", capture_output=True,
                                    encoding="utf-8", errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError("SSH RPC timed out; remote outcome is unknown. Query state before retrying a submission.")
        try:
            response = json.loads(result.stdout.strip())
        except ValueError:
            raise RuntimeError("SSH/RPC did not return valid JSON: " + result.stderr[-2000:] + result.stdout[-1000:])
        if not response.get("ok"):
            raise RemoteRejected(response.get("error", "Remote error"))
        if result.returncode:
            raise RuntimeError("SSH exited with code " + str(result.returncode))
        return response["result"]

    def rpc(self, request):
        name = request.get("project")
        if isinstance(name, dict):
            node = name.get("node")
        elif name:
            node = read_json(self.root / "projects" / (identifier(name) + ".json"), {}).get("node")
        elif request.get("job"):
            submission = read_json(self.root / "jobs" / identifier(request["job"]) / "submission.json", {})
            node = submission.get("node") or (read_json(self.root / "projects" / (identifier(submission['project']) + '.json'), {}).get('node') if submission.get('project') else None)
        else:
            node = None
        if node and self.node and node != self.node:
            raise ValueError("Explicit node differs from registered project/job node")
        if node and not self.node:
            return Harness(self.root, node).rpc(request)
        config = self.config()
        if config.get("platform", "windows") == "posix":
            import shlex
            script = shlex.join([config["remote_python"], "-X", "utf8", "-u", config["remote_root"].rstrip("/") + "/worker.py", "rpc"])
            return self.transport(script, request)
        script = "$ProgressPreference='SilentlyContinue'; $ErrorActionPreference='Stop'; "
        script += "[Console]::InputEncoding=[Text.UTF8Encoding]::new($false); "
        script += "& " + ps_literal(config["remote_python"]) + " -X utf8 -u " + ps_literal(str(PureWindowsPath(config["remote_root"]) / "worker.py")) + " rpc; exit $LASTEXITCODE"
        return self.transport(script, request)

    def initialize(self, connection):
        config = read_json(connection)
        if not config:
            raise ValueError("Connection file missing")
        for field in ("identity_file", "known_hosts_file"):
            path = Path(config[field]).resolve()
            if not path.is_file():
                raise ValueError(field + " missing")
            config[field] = str(path)
        # Host/user are arguments to OpenSSH: reject options and command-like destinations.
        import re
        if not re.fullmatch(r"[a-zA-Z0-9.-]+", config["host"]) or not re.fullmatch(r"[a-zA-Z0-9_.-]+", config["user"]):
            raise ValueError("Invalid SSH host/user")
        config.setdefault("remote_python", "python3" if config.get("platform") == "posix" else "D:\\miniconda3\\python.exe")
        if "remote_root" not in config:
            if config.get("platform") == "posix":
                raise ValueError("POSIX node requires an explicit absolute remote_root")
            config["remote_root"] = str(PureWindowsPath(config["remote_workspace"]) / "work" / "lab-harness")
        from pathlib import PurePosixPath
        if config.get("platform", "windows") not in {"windows", "posix"}:
            raise ValueError("Supported platforms: windows, posix")
        if not isinstance(config.get("port", 22), int) or not 1 <= config.get("port", 22) <= 65535:
            raise ValueError("Invalid SSH port")
        root = (PureWindowsPath if config.get("platform", "windows") == "windows" else PurePosixPath)(config["remote_root"])
        if not root.is_absolute() or root == type(root)(root.anchor):
            raise ValueError("Unsafe remote root")
        files = {name: base64.b64encode((SOURCE / name).read_bytes()).decode("ascii") for name in ("common.py", "worker.py")}
        script = "$ProgressPreference='SilentlyContinue'; $ErrorActionPreference='Stop'; [Console]::InputEncoding=[Text.UTF8Encoding]::new($false); "
        payload = {"installer": (SOURCE / "installer.py").read_text(encoding="utf-8"), "files": files}
        # Send source on stdin, not Windows' length-limited SSH command line.
        expression = "_deploy_payload=__import__('json').loads(__import__('sys').stdin.buffer.readline().decode('utf-8'));exec(_deploy_payload['installer'])"
        script += "& " + ps_literal(config["remote_python"]) + " -X utf8 -c " + ps_literal(expression) + " " + ps_literal(config["remote_root"]) + "; exit $LASTEXITCODE"
        if config.get("platform", "windows") == "posix":
            import shlex
            script = shlex.join([config["remote_python"], "-X", "utf8", "-c", expression, config["remote_root"]])
        deployed = self.transport(script, payload, config)
        write_json(self.root / "nodes" / (identifier(self.node) + ".json") if self.node else self.root / "connection.json", config)
        probe = self.rpc({"action": "probe"})
        probe["deployment"] = deployed
        return probe

    def project(self, name):
        data = read_json(self.root / "projects" / (identifier(name) + ".json"))
        if not data:
            raise ValueError("Unknown local project")
        if data.get("configuration_pending"):
            canonical = self.rpc({"action": "project", "project": name})["project"]
            for key in ("model", "effort", "timeout"):
                data[key] = canonical[key]
            data.pop("configuration_pending", None)
            write_json(self.root / "projects" / (name + ".json"), data)
        return data

    def register(self, name, local_path, remote_path, model, effort, timeout):
        identifier(name)
        path = Path(local_path).resolve()
        if not path.is_dir() or path == Path(path.anchor):
            raise ValueError("Local project must be an existing directory, not a drive root")
        data = {"name": name, "local_path": str(path), "remote_path": remote_path,
                "model": model, "effort": effort, "timeout": timeout}
        if self.node:
            data["node"] = self.node
        memory = {"revision": 0, "entries": [], "note": "Only attributed claims with evidence; no automatic promotion of worker reports to facts."}
        with lock(self.root / "registry.lock"):
            if (self.root / "projects" / (name + ".json")).exists():
                raise ValueError("Local project already exists")
            result = self.rpc({"action": "register", "project": data, "memory": memory})
            write_json(self.root / "projects" / (name + ".json"), data)
            write_json(self.root / "memory" / (name + ".json"), memory)
        return result

    def remember(self, name, kind, text, evidence):
        self.project(name)
        with lock(self.root / "registry.lock"):
            # Read canonical remote revision to recover from a previous interrupted local write.
            remote = self.rpc({"action": "project", "project": name})
            memory = remote["memory"]
            revision = memory["revision"]
            memory["entries"].append({"kind": kind, "text": text, "evidence": evidence,
                                      "author": "controller", "recorded_at": datetime.now(timezone.utc).isoformat()})
            memory["revision"] += 1
            result = self.rpc({"action": "memory", "project": name, "expected_revision": revision, "memory": memory})
            write_json(self.root / "memory" / (name + ".json"), memory)
        return result

    def queue(self, name, text):
        self.project(name)
        with lock(self.root / "registry.lock"):
            path = self.root / "inbox" / (name + ".json")
            pending = read_json(path, [])
            pending.append({"id": uuid.uuid4().hex, "text": text, "queued_at": datetime.now(timezone.utc).isoformat()})
            write_json(path, pending)
        return {"queued": len(pending), "delivery": "Included in the next submitted turn; no mid-turn interruption"}

    def submit(self, name, task, stage, allowed_files, acceptance, validation):
        project = self.project(name)
        if not task.strip() or not acceptance.strip() or not validation.strip():
            raise ValueError("Task, acceptance and validation are required")
        if stage == "implement" and not allowed_files.strip():
            raise ValueError("Implementation needs an explicit ownership scope")
        with lock(self.root / "registry.lock"):
            inbox = self.root / "inbox" / (name + ".json")
            pending = read_json(inbox, [])
            contract = json.dumps({"stage": stage, "objective": task, "allowed_files_and_systems": allowed_files or "No edits; project reading only",
                                   "forbidden_changes": "Other owners' files; credentials; system/network configuration; external messages; unrelated changes",
                                   "acceptance_criteria": acceptance, "validation_commands": validation,
                                   "required_return_format": "Findings/proposal, disagreements with evidence, files changed, commands and results, remaining risks",
                                   "controller_messages": pending}, ensure_ascii=False, indent=2)
            job = "job-" + uuid.uuid4().hex
            path = self.root / "jobs" / job
            write_json(path / "submission.json", {"job": job, "project": name, "stage": stage, "contract": contract, "state": "submission_pending"})
            try:
                result = self.rpc({"action": "start", "project": name, "job": job, "prompt": contract})
            except RemoteRejected as exc:
                write_json(path / "submission.json", {"job": job, "project": name, "stage": stage, "contract": contract,
                                                       "state": "submission_rejected", "error": str(exc)})
                raise RuntimeError(f"Submission rejected ({job}): {exc}")
            except Exception as exc:
                write_json(path / "submission.json", {"job": job, "project": name, "stage": stage, "contract": contract,
                                                       "state": "submission_unknown", "error": str(exc)})
                raise RuntimeError(f"Submission not confirmed ({job}); query this job before retrying: {exc}")
            write_json(path / "submission.json", {"job": job, "project": name, "stage": stage, "contract": contract, "state": "submitted"})
            write_json(inbox, [])
        return result

    def status(self, job, fetch=False):
        identifier(job)
        result = self.rpc({"action": "fetch" if fetch else "status", "job": job})
        write_json(self.root / "jobs" / job / "result.json", result)
        if fetch:
            (self.root / "jobs" / job / "report.txt").write_text(result.get("report", ""), encoding="utf-8")
        return result

    def review(self, job, verdict, evidence):
        if verdict not in {"accepted", "changes_requested"}:
            raise ValueError("Invalid verdict")
        result = self.status(job, fetch=True)
        if result["status"] != "needs_review":
            raise ValueError("Only a completed needs_review job can receive a verdict")
        if not evidence.strip():
            raise ValueError("Independent review evidence is required")
        receipt = {"job": job, "verdict": verdict, "evidence": evidence,
                   "reviewer": "controller", "reviewed_at": datetime.now(timezone.utc).isoformat()}
        write_json(self.root / "jobs" / job / "review.json", receipt)
        return receipt

    def configure(self, name, model, effort, timeout):
        with lock(self.root / "registry.lock"):
            data = self.project(name)
            data["configuration_pending"] = True
            write_json(self.root / "projects" / (identifier(name) + ".json"), data)
            try:
                result = self.rpc({"action": "configure", "project": name, "model": model, "effort": effort, "timeout": timeout})
            except RemoteRejected:
                data.pop("configuration_pending", None)
                write_json(self.root / "projects" / (identifier(name) + ".json"), data)
                raise
            data.update(model=model, effort=effort, timeout=timeout)
            data.pop("configuration_pending", None)
            write_json(self.root / "projects" / (identifier(name) + ".json"), data)
        return result

    def snapshot(self):
        nodes = [{"name": p.stem, **{k: v for k, v in read_json(p).items() if k in {"host", "user", "platform", "port"}}} for p in (self.root / "nodes").glob("*.json")]
        projects = [read_json(p) for p in (self.root / "projects").glob("*.json")]
        jobs = []
        for p in (self.root / "jobs").glob("*/submission.json"):
            item = read_json(p)
            result = read_json(p.parent / "result.json", {})
            item.update({k: v for k, v in result.items() if k not in {"events", "report", "stderr"}})
            item["review"] = read_json(p.parent / "review.json")
            jobs.append(item)
        return {"nodes": nodes, "projects": projects, "jobs": jobs[-200:], "capabilities": {"executor": "codex-exec", "mid_turn_steering": False, "idle_chat_wakeup": False, "messages": "next turn"}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", default=str(DEFAULT_STATE))
    parser.add_argument("--node", help="Registered SSH node")
    subs = parser.add_subparsers(dest="action", required=True)
    init = subs.add_parser("init")
    init.add_argument("--connection", required=True)
    subs.add_parser("probe")
    subs.add_parser("list")
    configure = subs.add_parser("configure")
    configure.add_argument("name")
    configure.add_argument("--model", required=True)
    configure.add_argument("--effort", required=True)
    configure.add_argument("--timeout", type=int, default=1800)
    subs.add_parser("demo-path")
    reg = subs.add_parser("register")
    reg.add_argument("name")
    reg.add_argument("--local-path", required=True)
    reg.add_argument("--remote-path", required=True)
    reg.add_argument("--model", required=True)
    reg.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max", "ultra"])
    reg.add_argument("--timeout", type=int, default=1800)
    rem = subs.add_parser("remember")
    rem.add_argument("name")
    rem.add_argument("--kind", choices=["fact", "decision", "question"], required=True)
    rem.add_argument("--text", required=True)
    rem.add_argument("--evidence", required=True)
    queue = subs.add_parser("message")
    queue.add_argument("name")
    queue.add_argument("--file", required=True)
    submit = subs.add_parser("submit")
    submit.add_argument("name")
    submit.add_argument("--task-file", required=True)
    submit.add_argument("--stage", choices=["discuss", "implement", "review"], required=True)
    submit.add_argument("--allowed-files", default="")
    submit.add_argument("--acceptance", required=True)
    submit.add_argument("--validation", required=True)
    for verb in ("status", "fetch", "cancel", "wait"):
        command = subs.add_parser(verb)
        command.add_argument("job")
        if verb == "wait":
            command.add_argument("--seconds", type=int, default=30)
    review = subs.add_parser("review-result")
    review.add_argument("job")
    review.add_argument("--verdict", choices=["accepted", "changes_requested"], required=True)
    review.add_argument("--evidence", required=True)
    args = parser.parse_args()
    harness = Harness(args.state, args.node)
    if args.action == "init":
        result = harness.initialize(args.connection)
    elif args.action == "probe":
        result = harness.rpc({"action": "probe"})
    elif args.action == "demo-path":
        result = harness.rpc({"action": "mkdir_demo"})
    elif args.action == "list":
        result = {"projects": [read_json(p) for p in (harness.root / "projects").glob("*.json")],
                  "jobs": [read_json(p) for p in (harness.root / "jobs").glob("*/submission.json")]}
    elif args.action == "register":
        result = harness.register(args.name, args.local_path, args.remote_path, args.model, args.effort, args.timeout)
    elif args.action == "configure":
        result = harness.configure(args.name, args.model, args.effort, args.timeout)
    elif args.action == "remember":
        result = harness.remember(args.name, args.kind, args.text, args.evidence)
    elif args.action == "message":
        result = harness.queue(args.name, Path(args.file).read_text(encoding="utf-8-sig"))
    elif args.action == "submit":
        result = harness.submit(args.name, Path(args.task_file).read_text(encoding="utf-8-sig"), args.stage, args.allowed_files, args.acceptance, args.validation)
    elif args.action == "cancel":
        result = harness.rpc({"action": "cancel", "job": identifier(args.job)})
    elif args.action == "review-result":
        result = harness.review(args.job, args.verdict, args.evidence)
    elif args.action == "wait":
        if not 0 <= args.seconds <= 60:
            raise ValueError("Wait duration must be 0-60 seconds; repeat bounded waits")
        deadline = time.monotonic() + args.seconds
        while True:
            result = harness.status(args.job)
            if result["status"] in TERMINAL or time.monotonic() >= deadline:
                break
            time.sleep(min(2, max(0, deadline - time.monotonic())))
    else:
        result = harness.status(args.job, fetch=args.action == "fetch")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    try:
        main()
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
