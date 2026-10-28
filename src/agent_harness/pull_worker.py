"""Locally owned executor. The broker supplies work, never machine configuration."""

import argparse
import csv
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from agent_harness.common import (
    identifier,
    lock,
    read_json,
    validate_project,
    write_json,
)
from agent_harness.protocol import ensure_hub_compatible
from agent_harness.release_info import version_path
from agent_harness.broker_transport import broker_opener
from agent_harness.health import HealthCode, component, report

TERMINAL = {
    "needs_review",
    "completed",
    "failed",
    "cancelled",
    "timed_out",
    "interrupted",
    "unknown",
}
LOCAL_RESULT_RETENTION_SECONDS = 30 * 86400


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def deploy_runtime(root, source_root=None):
    root = Path(root).resolve()
    source_root = Path(source_root or Path(__file__).resolve().parent).resolve()
    root.mkdir(parents=True, exist_ok=True)
    with lock(root / "registry.lock"):
        if any(read_json(p) for p in (root / "projects").glob("*/active.json")):
            raise ValueError("Reconcile active project supervisors before updating this runtime")
        for directory in ("jobs", "executions"):
            for state in (root / directory).glob("*/state.json"):
                if read_json(state, {}).get("status") not in TERMINAL - {"unknown"}:
                    raise ValueError("Cannot update a runtime with active jobs")
        for name in (
            "common.py",
            "worker.py",
            "jobrunner.py",
            "warm_worker.py",
            "appserver_client.py",
            "release_info.py",
            "VERSION",
        ):
            source = source_root / name
            if name == "VERSION" and not source.is_file():
                source = version_path()
                if source is None:
                    raise FileNotFoundError("Application VERSION is missing")
            target = root / name
            if source != target:
                shutil.copyfile(source, target)


def api(config, operation, payload=None):
    validate_server(config["server"])
    url = config["server"].rstrip("/") + "/api/device/" + operation
    headers = {"Content-Type": "application/json"}
    if operation != "enroll":
        headers["Authorization"] = "Bearer " + config["device_token"]
    request = urllib.request.Request(url, data=json.dumps(payload or {}).encode(), headers=headers, method="POST")
    # System proxy configuration must not intercept the owner's explicit broker route.
    with broker_opener(config.get("broker_proxy")).open(request, timeout=15) as response:
        data = json.load(response)
    if data.get("ok") is False:
        raise ValueError(data.get("error", "Broker rejected request"))
    return data.get("result", data)


def validate_server(server):
    parsed = urllib.parse.urlsplit(server)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Provide an explicit HTTP(S) broker URL without credentials")
    if parsed.scheme != "https" and parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise ValueError("A remote broker requires HTTPS; HTTP is allowed only on loopback")
    if parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ValueError("Broker URL must contain only its origin")


def enroll(server, pair_code, root, config_path=None, broker_proxy=None):
    validate_server(server)
    root = Path(root).resolve()
    deploy_runtime(root)
    config_path = Path(config_path or root / "pull-config.json")
    if config_path.exists():
        raise ValueError("Connector is already enrolled; retain its existing device identity")
    config = {
        "server": server.rstrip("/"),
        "root": str(root),
        "device_token": secrets.token_urlsafe(48),
    }
    if broker_proxy:
        broker_opener(broker_proxy)
        config["broker_proxy"] = broker_proxy
    result = api(
        config,
        "enroll",
        {"pair_code": pair_code, "device_token": config["device_token"]},
    )
    config["node"] = result.get("node", result.get("name"))
    write_json(config_path, config)
    try:
        if os.name == "nt":
            # Limit the bearer identity to this OS user and SYSTEM. Never print it.
            no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            who = subprocess.check_output(
                ["whoami.exe", "/user", "/fo", "csv", "/nh"],
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=no_window,
            )
            sid = next(csv.reader(who.splitlines()))[-1]
            result_acl = subprocess.run(
                [
                    "icacls.exe",
                    str(config_path),
                    "/inheritance:r",
                    "/grant:r",
                    "*" + sid + ":F",
                    "*S-1-5-18:F",
                ],
                capture_output=True,
                timeout=15,
                creationflags=no_window,
            )
            if result_acl.returncode:
                raise RuntimeError("Device credential ACL failed")
        else:
            config_path.chmod(0o600)
    except Exception:
        config_path.unlink(missing_ok=True)
        raise RuntimeError(
            "Enrollment credential was removed because local permissions failed; generate a new pairing code"
        ) from None
    if not (root / "local-policy.json").exists():
        write_json(root / "local-policy.json", {"bindings": []})
    return {
        "node": config["node"],
        "config": str(config_path),
        "next": "Open the local console and explicitly enable project bindings",
    }


class Connector:
    def __init__(self, config_path, transport=api, executor=None):
        self.config_path = Path(config_path)
        self.config = read_json(self.config_path)
        if not self.config:
            raise ValueError("Enroll this machine first")
        validate_server(self.config["server"])
        self.root = Path(self.config["root"]).resolve()
        self.transport = transport
        self.executor = executor or self.rpc
        self.headless_transfers = None

    def rpc(self, request):
        result = subprocess.run(
            [sys.executable, "-X", "utf8", str(self.root / "worker.py")],
            input=json.dumps(request),
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=40,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        data = json.loads(result.stdout)
        if not data.get("ok"):
            raise ValueError(data.get("error", "Local executor failed"))
        return data["result"]

    def bindings(self):
        return read_json(self.root / "local-policy.json", {"bindings": []})["bindings"]

    def save_binding(self, binding):
        # This method is reachable only from the local console/CLI, never a broker job.
        binding = dict(binding)
        identifier(binding["backend_project"])
        binding["name"] = str(binding.get("name") or binding["backend_project"]).strip() or binding["backend_project"]
        if not isinstance(binding.get("project_id"), str) or not binding["project_id"]:
            raise ValueError("Choose a shared project")
        project = read_json(self.root / "projects" / binding["backend_project"] / "project.json")
        if not project:
            raise ValueError("Register the local working directory first")
        configured = dict(
            project,
            model=binding["model"],
            effort=binding["effort"],
            timeout=int(binding.get("timeout", 120)),
        )
        validate_project(configured)
        stages = binding.get("stages", ["discuss"])
        if not isinstance(stages, list) or not stages or set(stages) - {"discuss", "review", "implement"}:
            raise ValueError("Invalid locally permitted stages")
        binding.update(
            timeout=configured["timeout"],
            enabled=bool(binding.get("enabled")),
            stages=stages,
        )
        with lock(self.root / "local-policy.lock"):
            bindings = self.bindings()
            if any(
                x["backend_project"] == binding["backend_project"] and x["project_id"] != binding["project_id"]
                for x in bindings
            ):
                raise ValueError("A local execution project cannot share sessions across shared projects")
            bindings = [x for x in bindings if x["backend_project"] != binding["backend_project"]]
            bindings.append(binding)
            write_json(self.root / "local-policy.json", {"bindings": bindings})
        return binding

    def advertisements(self):
        result = []
        for binding in self.bindings():
            item = {
                k: binding[k]
                for k in (
                    "project_id",
                    "backend_project",
                    "model",
                    "effort",
                    "timeout",
                    "enabled",
                    "stages",
                )
            }
            item["name"] = binding.get("name", binding["backend_project"])
            item["templates"] = list(
                read_json(
                    self.root / "projects" / binding["backend_project"] / "execution_templates.json",
                    {},
                )
            )
            result.append(item)
        return result

    def receipt_path(self, job_id):
        return self.root / "pull-receipts" / (hashlib.sha256(job_id.encode()).hexdigest() + ".json")

    def prune_local_results(self, now=None):
        """Remove old, centrally reported receipts and their fixed local job directories."""

        now = time.time() if now is None else now
        removed = 0
        receipts = self.root / "pull-receipts"
        for path in receipts.glob("*.json"):
            receipt = read_json(path, {})
            if not receipt.get("reported_terminal"):
                continue
            finished = receipt.get("finished_at")
            if not isinstance(finished, (int, float)):
                try:
                    finished = path.stat().st_mtime
                except OSError:
                    continue
            if finished > now - LOCAL_RESULT_RETENTION_SECONDS:
                continue
            local_id = receipt.get("local_id")
            if isinstance(local_id, str) and re.fullmatch(r"pull-[a-f0-9]{48}", local_id):
                area = "executions" if receipt.get("job", {}).get("kind") == "execution" else "jobs"
                parent = (self.root / area).resolve()
                target = (parent / local_id).resolve()
                if target.parent == parent and target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
            path.unlink(missing_ok=True)
            removed += 1
        return removed

    def approve(self, job_id, approved, expected_template=None):
        with lock(self.root / "pull-receipts.lock"):
            path = self.receipt_path(job_id)
            receipt = read_json(path)
            if not receipt or receipt["job"]["kind"] != "execution" or receipt["phase"] != "waiting_local_approval":
                raise ValueError("No execution awaiting local approval")
            receipt["approval"] = bool(approved)
            if approved:
                project = receipt["job"]["backend_project"]
                receipt["approved_template"] = read_json(
                    self.root / "projects" / project / "execution_templates.json", {}
                ).get(receipt["job"]["request"]["template"])
                if expected_template is not None and receipt["approved_template"] != expected_template:
                    raise ValueError("Template changed; review the current command before approving")
            write_json(path, receipt)

    def resolve(self, job_id, confirm_no_job=False):
        """Owner-triggered reconciliation. Never launch or repeat an unknown job."""
        with lock(self.root / "pull-receipts.lock"):
            path = self.receipt_path(job_id)
            receipt = read_json(path)
            if not receipt or receipt.get("result", {}).get("status") != "unknown":
                raise ValueError("This job is not awaiting local reconciliation")
            execution = receipt["job"]["kind"] == "execution"
            local_id = receipt["local_id"]
            try:
                result = self.executor(
                    {
                        "action": "execution_status" if execution else "fetch",
                        "execution" if execution else "job": local_id,
                    }
                )
            except Exception:
                local_directory = self.root / ("executions" if execution else "jobs") / local_id
                if not confirm_no_job or local_directory.exists():
                    raise ValueError(
                        "No confirmed result. Inspect the local process/state; explicitly confirm no job only when its directory is absent"
                    )
                result = {
                    "status": "cancelled",
                    "error": "Local owner confirmed no job exists; unknown assignment closed without restarting",
                }
            receipt.update(
                phase="terminal" if result.get("status") in TERMINAL - {"unknown"} else "running",
                result={
                    k: v
                    for k, v in result.items()
                    if k
                    in {
                        "status",
                        "report",
                        "error",
                        "exit_code",
                        "stdout_tail",
                        "stderr_tail",
                    }
                },
                reported_terminal=False,
            )
            write_json(path, receipt)
            return {"job_id": job_id, "status": result.get("status")}

    def binding_for(self, job):
        bindings = [
            x
            for x in self.bindings()
            if x["project_id"] == job["project_id"] and x["backend_project"] == job["backend_project"]
        ]
        if len(bindings) != 1 or not bindings[0].get("enabled"):
            raise ValueError("This project is not enabled by the local owner")
        return bindings[0]

    def accept(self, job):
        job_id = str(job.get("job_id", job.get("id")))
        if not job_id or job_id == "None":
            raise ValueError("Missing durable job identity")
        digest = hashlib.sha256(
            json.dumps(
                {k: v for k, v in job.items() if k not in {"lease_id", "state", "status"}},
                sort_keys=True,
            ).encode()
        ).hexdigest()
        path = self.receipt_path(job_id)
        receipt = read_json(path)
        if receipt:
            if receipt["digest"] != digest or receipt["job"]["lease_id"] != job["lease_id"]:
                raise ValueError("Broker changed an already received assignment")
            return receipt
        receipt = {
            "job": job,
            "digest": digest,
            "local_id": "pull-" + hashlib.sha256(job_id.encode()).hexdigest()[:48],
            "phase": "received",
        }
        write_json(path, receipt)  # BEFORE any process launch; crash recovery never blindly starts again.
        return receipt

    def configure(self, binding):
        self.executor(
            {
                "action": "configure",
                "project": binding["backend_project"],
                **{k: binding[k] for k in ("model", "effort", "timeout")},
            }
        )
        with lock(self.root / "registry.lock"):
            path = self.root / "projects" / binding["backend_project"] / "project.json"
            project = read_json(path)
            project["local_control"] = True
            write_json(path, project)

    def advance(self, receipt, cancelled=False):
        job = receipt["job"]
        request = job["request"]
        local_id = receipt["local_id"]
        path = self.receipt_path(str(job.get("job_id", job.get("id"))))
        if receipt["phase"] == "terminal":
            return receipt
        try:
            execution = job["kind"] == "execution"
            if job["kind"] not in {"task", "execution"}:
                raise ValueError("Unsupported assignment kind")
            if set(request) - (
                {"template"}
                if execution
                else {
                    "prompt",
                    "stage",
                    "scope",
                    "acceptance",
                    "validation",
                    "session_channel",
                }
            ):
                raise ValueError("Broker assignment contains forbidden configuration")
            binding = self.binding_for(job) if receipt["phase"] in {"received", "waiting_local_approval"} else None
            if cancelled and receipt["phase"] in {"received", "waiting_local_approval"}:
                receipt.update(phase="terminal", result={"status": "cancelled"})
            elif receipt["phase"] == "received":
                if execution:
                    templates = read_json(
                        self.root / "projects" / binding["backend_project"] / "execution_templates.json",
                        {},
                    )
                    if request.get("template") not in templates:
                        raise ValueError("Template is not registered by the local owner")
                    receipt["phase"] = "waiting_local_approval"
                else:
                    stage = request.get("stage", "discuss")
                    if stage not in binding["stages"]:
                        raise ValueError("Stage is not permitted by the local owner")
                    context = json.loads(request["prompt"])
                    if (
                        context.get("identity", {}).get("id") != job["agent_id"]
                        or context.get("project", {}).get("id") != job["project_id"]
                    ):
                        raise ValueError("Assignment identity does not match its local binding")
                    self.configure(binding)
                    receipt["phase"] = "launching"
                    write_json(path, receipt)
                    contract = {
                        "stage": stage,
                        "objective": request["prompt"],
                        "allowed_files_and_systems": request.get("scope", "No edits"),
                        "acceptance": request.get("acceptance", ""),
                        "validation": request.get("validation", ""),
                    }
                    self.executor(
                        {
                            "action": "start",
                            "project": binding["backend_project"],
                            "job": local_id,
                            "prompt": json.dumps(contract),
                            "session_channel": identifier(request.get("session_channel") or "group"),
                        }
                    )
                    receipt["phase"] = "running"
            elif receipt["phase"] == "waiting_local_approval":
                if receipt.get("approval") is False:
                    receipt.update(
                        phase="terminal",
                        result={
                            "status": "cancelled",
                            "error": "Rejected by local owner",
                        },
                    )
                elif receipt.get("approval") is True:
                    current = read_json(
                        self.root / "projects" / binding["backend_project"] / "execution_templates.json",
                        {},
                    ).get(request["template"])
                    if not current or current != receipt.get("approved_template"):
                        raise ValueError("Template changed after local approval; submit a new request")
                    self.configure(binding)
                    receipt["phase"] = "launching"
                    write_json(path, receipt)
                    self.executor(
                        {
                            "action": "execution_start",
                            "project": binding["backend_project"],
                            "execution": local_id,
                            "template": request["template"],
                        }
                    )
                    receipt["phase"] = "running"
            else:
                # launching means a process may have been created before a crash: reconcile its fixed ID.
                if cancelled:
                    self.executor(
                        {
                            "action": "execution_cancel" if execution else "cancel",
                            "execution" if execution else "job": local_id,
                        }
                    )
                result = self.executor(
                    {
                        "action": "execution_status" if execution else "fetch",
                        "execution" if execution else "job": local_id,
                    }
                )
                receipt["phase"] = "terminal" if result.get("status") in TERMINAL else "running"
                receipt["result"] = {
                    k: result[k]
                    for k in (
                        "status",
                        "report",
                        "reply_draft",
                        "error",
                        "exit_code",
                        "stdout_tail",
                        "stderr_tail",
                        "requested_model",
                        "reasoning_effort",
                        "effective_model",
                        "model_verified",
                    )
                    if k in result and (k != "reply_draft" or isinstance(result[k], str) and len(result[k]) <= 16000)
                }
        except Exception as exc:
            if receipt["phase"] in {"running", "launching"}:
                # Observation failures are not cancellation authority.  Keep
                # the durable local ID and reconcile it on a later cycle.
                receipt.update(
                    phase="running",
                    result={"status": "unknown", "error": str(exc)[:2000]},
                )
                write_json(path, receipt)
                return receipt
            receipt.update(
                phase="terminal",
                result={
                    "status": "unknown" if receipt["phase"] in {"launching", "running"} else "failed",
                    "error": str(exc)[:2000],
                },
            )
        write_json(path, receipt)
        return receipt

    def cycle(self, wait_seconds=10):
        health = self._health()
        data = self.transport(
            self.config,
            "poll",
            {"bindings": self.advertisements(), "wait_seconds": wait_seconds, "health": health},
        )
        ensure_hub_compatible(data)
        write_json(self.root / "broker-projects.json", data.get("projects", []))
        if data.get("owner_commands"):
            from agent_harness.owner_worker import OwnerControl

            if not self.config.get("owner_account_id"):
                raise PermissionError("This receiver has no enrolled account identity; pair it again")
            for envelope in data["owner_commands"]:
                result = OwnerControl(self).apply(envelope)
                self.transport(self.config, "owner-report", dict(result, id=envelope["body"]["id"]))
        if sys.platform.startswith("linux") and "device_file_transfers_v1" in data.get("features", []):
            if self.headless_transfers is None:
                from agent_harness.headless_transfers import HeadlessTransferReceiver

                self.headless_transfers = HeadlessTransferReceiver(self)
            # Downloads run on independent background threads and never hold
            # task, message, or owner-command polling open.
            try:
                self.headless_transfers.reconcile()
            except (OSError, RuntimeError, ValueError):
                # File delivery is independent from Agent work. A transient
                # relay problem must not prevent jobs or commands being polled.
                pass
        cancellations = set(data.get("cancellations", []))
        with lock(self.root / "pull-receipts.lock"):
            for job in data.get("jobs", []):
                self.accept(job)
            for path in (self.root / "pull-receipts").glob("*.json"):
                receipt = read_json(path)
                job = receipt["job"]
                job_id = str(job.get("job_id", job.get("id")))
                if receipt.get("reported_terminal"):
                    continue
                # A local owner may have reconciled an unknown receipt to a confirmed terminal result.
                receipt = self.advance(receipt, job_id in cancellations)
                result = receipt.get("result", {"status": "running", "local_status": receipt["phase"]})
                if "stdout_tail" in result:
                    result["stdout"] = result.pop("stdout_tail")
                if "stderr_tail" in result:
                    result["stderr"] = result.pop("stderr_tail")
                self.transport(
                    self.config,
                    "report",
                    {
                        "job_id": job_id,
                        "lease_id": job["lease_id"],
                        "state": result.get("status", "running") if receipt["phase"] == "terminal" else "running",
                        "result": result,
                    },
                )

                if receipt["phase"] == "terminal":
                    receipt["reported_terminal"] = True
                    receipt["finished_at"] = time.time()
                    write_json(path, receipt)
            self.prune_local_results()

    def _health(self):
        """Build a privacy-bounded heartbeat; paths and exception text stay local."""
        components = [component("receiver", "healthy", HealthCode.OK, "接收端运行正常")]
        cli = read_json(self.root / "codex-cli.json", {})
        if not isinstance(cli, dict) or not isinstance(cli.get("path"), str) or not cli["path"]:
            components.append(component("codex", "degraded", HealthCode.NOT_CONFIGURED, "尚未配置 Codex"))
        else:
            components.append(component("codex", "healthy", HealthCode.OK, "Codex 已配置"))
        return report(components)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=["pair", "enroll", "run", "loop"])
    parser.add_argument("--server")
    parser.add_argument("--pair-code")
    parser.add_argument("--root")
    parser.add_argument("--config")
    parser.add_argument("--policy", help="Local binding JSON file to import")
    parser.add_argument("--approve")
    parser.add_argument("--reject")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--resolve")
    parser.add_argument("--confirm-no-job", action="store_true")
    args = parser.parse_args()
    if args.pair_code:
        if not args.server or not args.root:
            parser.error("Enrollment requires --server and --root")
        print(json.dumps(enroll(args.server, args.pair_code, args.root, args.config)))
        return
    if not args.config:
        parser.error("Use --config pointing to the local connector configuration")
    connector = Connector(args.config)
    if args.resolve:
        print(json.dumps(connector.resolve(args.resolve, args.confirm_no_job)))
        return
    if args.policy:
        for binding in read_json(args.policy)["bindings"]:
            connector.save_binding(binding)
        print("Local policy saved")
        return
    if args.approve or args.reject:
        connector.approve(args.approve or args.reject, bool(args.approve))
        print("Local decision saved")
        return
    with lock(connector.root / "pull-worker.lock"):
        while True:
            try:
                connector.cycle(0 if args.once else 10)
            except Exception as exc:
                write_json(
                    connector.root / "connector-status.json",
                    {
                        "online": False,
                        "error": type(exc).__name__,
                        "updated": time.time(),
                    },
                )
            else:
                write_json(
                    connector.root / "connector-status.json",
                    {"online": True, "updated": time.time()},
                )
            if args.once:
                break
            time.sleep(1)


if __name__ == "__main__":
    main()
