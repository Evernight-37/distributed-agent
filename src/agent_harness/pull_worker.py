"""Locally owned executor. The broker supplies work, never machine configuration."""

import argparse
import csv
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
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
from agent_harness.execution_approvals import execution_digest, valid_execution_digest
from agent_harness.health import HealthCode, component, report
from agent_harness.workspace_ownership import DIRTY_WORKTREE

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
PROJECT_TOOL_REQUEST = re.compile(r"pt-request-([a-f0-9]{32})\.json")
PROJECT_TOOL_RESULT = "pt-result-{}.json"
PROJECT_TOOL_MAX_FILES = 32
PROJECT_TOOL_MAX_BYTES = 64 * 1024


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
    from agent_harness import sealing

    config["encrypt_key"] = sealing.generate_keypair()
    if broker_proxy:
        broker_opener(broker_proxy)
        config["broker_proxy"] = broker_proxy
    result = api(
        config,
        "enroll",
        {
            "pair_code": pair_code,
            "device_token": config["device_token"],
            "encrypt_public_key": config["encrypt_key"]["public_key"],
        },
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
        self.root = self._resolve_root()
        self.transport = transport
        self.executor = executor or self.rpc

    def _resolve_root(self):
        """Where this receiver's mutable state lives.

        Enrollment writes the configuration inside the receiver directory, so that
        directory is the truth. A stored absolute path is only trusted when the
        configuration does not sit in a receiver root at all: moving user data
        leaves the recorded path pointing at the old location, and honouring it
        would silently recreate a second receiver there.
        """
        here = self.config_path.resolve().parent
        recorded = self.config.get("root")
        if not recorded:
            return here
        stored = Path(recorded).resolve()
        if (here / "worker.py").is_file():
            if stored != here:
                self.config["root"] = str(here)
                write_json(self.config_path, self.config)
            return here
        return stored

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

    def _consume_transfers(self, transfers):
        """Receive files a teammate offers; the service never holds the bytes."""
        from agent_harness import transfer_trust

        node = self.config.get("node")
        folder = self._transfer_folder()
        for row in transfers:
            if not isinstance(row, dict) or row.get("to_node") not in (None, node):
                continue
            if row.get("from_node") == node:
                # My own outgoing transfer, not something to receive.
                continue
            transfer_id = row.get("id")
            state = row.get("state")
            if state == "queued":
                # A default folder plus a sender the owner already accepted is the
                # only case that runs without asking; anything else stays pending.
                if not (folder and transfer_trust.is_trusted(self.root, row.get("owner_id"))):
                    self._remember_pending_transfer(row)
                    continue
                self.transport(
                    self.config,
                    "transfer-respond",
                    {
                        "transfer_id": transfer_id,
                        "accept": True,
                        "transfer_public_key": self.transfer_public_key(),
                    },
                )
                state = "accepted"
            if state != "accepted":
                continue
            from agent_harness.attachments import safe_name

            target = Path(folder) / safe_name(row.get("name") or "file")
            keys = self.transfer_signing_key()
            url = "http://" + str(row.get("address")) + "/transfer/" + str(transfer_id)
            try:
                self.transport(
                    self.config,
                    "transfer-progress",
                    {"transfer_id": transfer_id, "transferred": 0, "state": "running"},
                )
                self._pull_direct(url, keys, transfer_id, target, row.get("size"))
            except Exception as exc:  # one transfer must not break the poll loop
                self.transport(
                    self.config,
                    "transfer-progress",
                    {"transfer_id": transfer_id, "transferred": 0, "state": "failed", "error": str(exc)[:500]},
                )
                continue
            self.transport(
                self.config,
                "transfer-progress",
                {"transfer_id": transfer_id, "transferred": row.get("size") or 0, "state": "done"},
            )
            self._forget_pending_transfer(transfer_id)

    def _transfer_folder(self):
        """Where a received file lands: the configured folder, else a local default.

        A receiver fresh out of enrollment has no configured folder; falling back
        to `received-files` under its own root keeps same-account delivery working
        without an extra setup step.
        """
        settings = read_json(self.root / "file-transfer-settings.json", {})
        folder = settings.get("receive_folder") if isinstance(settings, dict) else None
        if isinstance(folder, str) and folder.strip():
            return Path(folder)
        from agent_harness.runtime_paths import received_files_home

        return received_files_home(self.root)

    def _pull_direct(self, url, keys, transfer_id, target, size):
        """Download, waiting for the sender to learn this device's key."""
        from agent_harness import direct_transfer

        direct_transfer.pull(
            url,
            keys["private_key"],
            keys["public_key"],
            transfer_id,
            target,
            size=size,
        )

    def _remember_pending_transfer(self, row):
        path = self.root / "pending-transfers.json"
        with lock(self.root / "pending-transfers.lock"):
            document = read_json(path, {"version": 1, "transfers": []})
            if not isinstance(document, dict) or not isinstance(document.get("transfers"), list):
                document = {"version": 1, "transfers": []}
            entries = [
                entry for entry in document["transfers"] if isinstance(entry, dict) and entry.get("id") != row.get("id")
            ]
            entries.append({key: row.get(key) for key in ("id", "from_node", "owner_id", "name", "size", "address")})
            write_json(path, {"version": 1, "transfers": entries[-50:]})

    def _forget_pending_transfer(self, transfer_id):
        path = self.root / "pending-transfers.json"
        with lock(self.root / "pending-transfers.lock"):
            document = read_json(path, {"version": 1, "transfers": []})
            if not isinstance(document, dict) or not isinstance(document.get("transfers"), list):
                return
            entries = [
                entry
                for entry in document["transfers"]
                if not (isinstance(entry, dict) and entry.get("id") == transfer_id)
            ]
            write_json(path, {"version": 1, "transfers": entries})

    def encryption_keypair(self):
        """The device's sealing key, created on demand and kept beside the token.

        Only the public half ever leaves this machine; the desktop seals owner
        payloads to it so the collaboration service cannot read them.
        """
        stored = self.config.get("encrypt_key")
        if isinstance(stored, dict) and stored.get("private_key") and stored.get("public_key"):
            return stored
        from agent_harness import sealing

        pair = sealing.generate_keypair()
        path = self.config_path
        with lock(path.with_suffix(".lock")):
            current = read_json(path, {})
            stored = current.get("encrypt_key")
            if isinstance(stored, dict) and stored.get("private_key") and stored.get("public_key"):
                self.config["encrypt_key"] = stored
                return stored
            current["encrypt_key"] = pair
            write_json(path, current)
            if os.name != "nt":
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass
        self.config["encrypt_key"] = pair
        return pair

    def encryption_public_key(self):
        return self.encryption_keypair()["public_key"]

    def transfer_signing_key(self):
        """The receiver's own signing identity for direct transfers.

        The pull worker has no account identity, so it carries its own key: the
        sender verifies requests signed with it before serving a byte.
        """
        stored = self.config.get("transfer_key")
        if isinstance(stored, dict) and stored.get("private_key") and stored.get("public_key"):
            return stored
        import base64

        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

        key = Ed25519PrivateKey.generate()
        pair = {
            "private_key": base64.b64encode(key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())).decode(
                "ascii"
            ),
            "public_key": base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode(
                "ascii"
            ),
        }
        path = self.config_path
        with lock(path.with_suffix(".lock")):
            current = read_json(path, {})
            stored = current.get("transfer_key")
            if isinstance(stored, dict) and stored.get("public_key"):
                self.config["transfer_key"] = stored
                return stored
            current["transfer_key"] = pair
            write_json(path, current)
            if os.name != "nt":
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass
        self.config["transfer_key"] = pair
        return pair

    def transfer_public_key(self):
        return self.transfer_signing_key()["public_key"]

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

    def resolve(self, job_id, confirm_no_job=False, *, kind=None):
        """Owner-triggered reconciliation. Never launch or repeat an unknown job."""
        with lock(self.root / "pull-receipts.lock"):
            path = self.receipt_path(job_id)
            receipt = read_json(path)
            if not receipt:
                raise ValueError("This job is not awaiting local reconciliation")
            job = receipt["job"]
            if kind is not None and job["kind"] != kind:
                raise ValueError("This assignment is not the expected job type")
            status = receipt.get("result", {}).get("status")
            if kind == "task" and receipt["phase"] == "terminal" and status in TERMINAL - {"unknown"}:
                return {"job_id": job_id, "status": status}
            if status != "unknown":
                raise ValueError("This job is not awaiting local reconciliation")
            execution = job["kind"] == "execution"
            local_id = receipt["local_id"]
            try:
                result = self.executor(
                    {
                        "action": "execution_status" if execution else "fetch",
                        "execution" if execution else "job": local_id,
                    }
                )
            except Exception as exc:
                local_directory = self.root / ("executions" if execution else "jobs") / local_id
                local_evidence = local_directory if execution else local_directory / "state.json"
                if confirm_no_job is not True or local_evidence.exists():
                    raise ValueError(
                        "No confirmed result. Check the local process before confirming the job is missing"
                    ) from exc
                if not execution:
                    if str(exc).strip() != "Unknown job":
                        raise ValueError("The receiver could not verify whether this task exists") from exc
                    backend = identifier(job["backend_project"])
                    active = read_json(self.root / "projects" / backend / "active.json", {}) or {}
                    if active.get("job") == local_id:
                        raise ValueError(
                            "This task still has a local active record; verify it has stopped before closing"
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
            if not execution and receipt["phase"] == "terminal":
                self._finalize_project_delivery(receipt, cancelled=False)
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

    def execution_snapshot(self, backend_project, template_name):
        path = self.root / "projects" / backend_project
        project = read_json(path / "project.json")
        template = read_json(path / "execution_templates.json", {}).get(template_name)
        if not template or not project:
            return template, None
        return template, execution_digest(project, template_name, template)

    def _project_tool_directory(self, receipt):
        """Return the isolated mailbox for one already-running local task.

        The model-facing tool can create request files only inside its task
        directory.  Keep every path check here so a crafted receipt or symlink
        cannot make the receiver read or write elsewhere in its data root.
        """

        local_id = receipt.get("local_id")
        if not isinstance(local_id, str) or not re.fullmatch(r"pull-[a-f0-9]{48}", local_id):
            return None
        parent = self.root.resolve() / "jobs"
        directory = parent / local_id
        if directory.is_symlink() or not directory.is_dir():
            return None
        try:
            if parent.resolve() != parent or directory.resolve().parent != parent:
                return None
        except OSError:
            return None
        return directory

    @staticmethod
    def _read_project_tool_action(path):
        """Read one bounded, regular request without following a symlink."""

        try:
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
        except OSError:
            return None, "任务工具请求无法读取"
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > PROJECT_TOOL_MAX_BYTES:
                return None, "任务工具请求格式无效"
            with os.fdopen(descriptor, "rb") as source:
                descriptor = None
                raw = source.read(PROJECT_TOOL_MAX_BYTES + 1)
        except OSError:
            return None, "任务工具请求无法读取"
        finally:
            if descriptor is not None:
                os.close(descriptor)
        if len(raw) > PROJECT_TOOL_MAX_BYTES:
            return None, "任务工具请求过长"
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None, "任务工具请求不是 JSON"
        if not isinstance(value, dict) or set(value) != {"action"} or not isinstance(value["action"], dict):
            return None, "任务工具请求只能包含 action 对象"
        return value["action"], None

    def _service_project_tools(self, receipt, cancelled):
        """Forward pending per-task tool calls over the task's existing lease.

        Results are retained in the mailbox for the executor to consume. A
        network failure leaves the request untouched so a later poll can retry
        the exact same request.  Hub validation failures are useful tool output
        and become a local JSON error response.
        """

        job = receipt.get("job") or {}
        if (
            cancelled
            or receipt.get("phase") != "running"
            or receipt.get("reported_terminal")
            or not receipt.get("project_tools")
            or job.get("kind") != "task"
            or not isinstance(job.get("job_id", job.get("id")), str)
            or not isinstance(job.get("lease_id"), str)
        ):
            return

        def rejected(code, message):
            # The local MCP bridge waits for this stable shape.  A malformed
            # request must give the caller a result, rather than waiting for its
            # timeout and losing the actionable validation message.
            return {"ok": False, "error_code": code, "message": message[:2000]}

        directory = self._project_tool_directory(receipt)
        if directory is None:
            return
        try:
            requests = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError:
            return
        handled = 0
        for request in requests:
            match = PROJECT_TOOL_REQUEST.fullmatch(request.name)
            if match is None:
                continue
            if handled >= PROJECT_TOOL_MAX_FILES:
                return
            handled += 1
            token = match.group(1)
            result_path = directory / PROJECT_TOOL_RESULT.format(token)
            if request.is_symlink() or result_path.exists():
                continue
            action, error = self._read_project_tool_action(request)
            if error:
                write_json(result_path, rejected("invalid_request", error))
                continue
            file_delivery = action.get("kind") == "deliver" and bool(action.get("attachments"))
            if file_delivery and not receipt.get("project_file_delivery"):
                write_json(result_path, rejected("action_not_allowed", "本轮交付工具不支持附件。"))
                continue
            try:
                result = self.transport(
                    self.config,
                    "project-tool",
                    {"job_id": job.get("job_id", job.get("id")), "lease_id": job["lease_id"], "action": action},
                )
            except (OSError, TimeoutError, urllib.error.URLError):
                # The request file is the idempotency record.  Keep it for the
                # next receiver cycle and never forge a successful response.
                continue
            except ValueError as exc:
                result = rejected("hub_rejected", str(exc))
            try:
                encoded = json.dumps(result, ensure_ascii=False)
            except (TypeError, ValueError):
                encoded = ""
            if not encoded or len(encoded.encode("utf-8")) > PROJECT_TOOL_MAX_BYTES:
                result = rejected("invalid_response", "任务工具响应无效或过长")
            if file_delivery and isinstance(result, dict) and result.get("ok") is True:
                summary = action.get("summary")
                expected = {
                    "summary": summary.strip() if isinstance(summary, str) else None,
                    "links": action.get("links"),
                    "attachments": action["attachments"],
                }
                if (
                    result.get("status") != "pending_attachment_delivery"
                    or result.get("delivery") != expected
                    or receipt.get("staged_file_delivery", expected) != expected
                ):
                    result = rejected("invalid_response", "Hub 返回的附件交付回执与提交内容不一致。")
                else:
                    # Preserve the checked reservation across a receiver restart
                    # before telling the executor it may finish this turn.
                    receipt["staged_file_delivery"] = expected
                    write_json(self.receipt_path(str(job.get("job_id", job.get("id")))), receipt)
                    result = {key: value for key, value in result.items() if key != "delivery"}
            write_json(result_path, result)

    @staticmethod
    def _finalize_project_delivery(receipt, cancelled):
        result = receipt["result"]
        if result.get("status") != "needs_review":
            return
        if staged := receipt.get("staged_file_delivery"):
            if cancelled:
                receipt["result"] = {"status": "cancelled", "error": "Cancelled before attachment delivery"}
                return
            result["report"] = json.dumps(
                {
                    "reply": "",
                    "project_action": {"kind": "deliver", "summary": staged["summary"], "links": staged["links"]},
                    "attachments": [{"path": path} for path in staged["attachments"]],
                    "project_file_delivery": True,
                },
                ensure_ascii=False,
            )
            return
        try:
            answer = json.loads(result.get("report") or "")
        except (ValueError, TypeError):
            return
        if isinstance(answer, dict):
            trusted = {
                "project_file_delivery",
                "delivery_attachment_count",
                "delivery_attachment_error",
                "attachment_ids",
            }
            if trusted.intersection(answer):
                result["report"] = json.dumps(
                    {key: value for key, value in answer.items() if key not in trusted}, ensure_ascii=False
                )

    def advance(
        self, receipt, cancelled=False, execution_decision=None, project_tools=False, project_file_delivery=False
    ):
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
                    _, snapshot = self.execution_snapshot(binding["backend_project"], request.get("template"))
                    if not snapshot:
                        raise ValueError("Template is not registered by the local owner")
                    receipt.update(phase="waiting_local_approval", execution_approval_digest=snapshot)
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
                    if "full_access" in (context.get("grants") or []):
                        approval = context.get("permission_request_id")
                        continuation = context.get("approved_continuation")
                        if (
                            not isinstance(approval, str)
                            or not re.fullmatch(r"permission-[0-9a-f]{32}", approval)
                            or not isinstance(continuation, dict)
                            or continuation.get("id") != approval
                        ):
                            raise PermissionError(
                                "This task has no one-time approval; legacy full access is no longer accepted"
                            )
                    self.configure(binding)
                    receipt["phase"] = "launching"
                    receipt["project_tools"] = bool(project_tools)
                    receipt["project_file_delivery"] = bool(project_tools and project_file_delivery)
                    write_json(path, receipt)
                    contract = {
                        "stage": stage,
                        "objective": request["prompt"],
                        "allowed_files": request.get("scope", "" if stage == "implement" else "No edits"),
                        "acceptance": request.get("acceptance", ""),
                        "validation": request.get("validation", ""),
                        "project_tools": bool(project_tools),
                        "project_file_delivery": receipt["project_file_delivery"],
                    }
                    try:
                        self.executor(
                            {
                                "action": "start",
                                "project": binding["backend_project"],
                                "job": local_id,
                                "prompt": json.dumps(contract),
                                "session_channel": identifier(request.get("session_channel") or "group"),
                            }
                        )
                    except ValueError as exc:
                        # These checks run under the executor lock before creating a job.
                        if str(exc) == DIRTY_WORKTREE:
                            receipt.update(phase="terminal", result={"status": "failed", "error": DIRTY_WORKTREE})
                        elif str(exc) == "Remote directory is busy in another project" or str(exc).startswith(
                            "Project already has an active job: "
                        ):
                            receipt.update(
                                phase="received", result={"status": "running", "local_status": "workspace_busy"}
                            )
                        else:
                            raise
                    else:
                        receipt["phase"] = "running"
                        receipt.pop("result", None)
            elif receipt["phase"] == "waiting_local_approval":
                current, snapshot = self.execution_snapshot(binding["backend_project"], request["template"])
                if snapshot and not receipt.get("execution_approval_digest"):
                    # Migrate waiting receipts from receivers that reported no digest.
                    receipt["execution_approval_digest"] = snapshot
                expected_digest = receipt.get("execution_approval_digest")
                delegated = (
                    receipt.get("approval") is None
                    and isinstance(execution_decision, dict)
                    and type(execution_decision.get("approved")) is bool
                    and valid_execution_digest(expected_digest)
                    and execution_decision.get("digest") == expected_digest
                )
                if delegated:
                    if execution_decision["approved"] and snapshot != expected_digest:
                        raise PermissionError("Approved execution snapshot changed; submit a new request")
                    receipt["approval"] = execution_decision["approved"]
                    receipt["approved_template"] = current
                    receipt["delegated_execution_digest"] = expected_digest
                if receipt.get("approval") is False:
                    receipt.update(
                        phase="terminal",
                        result={
                            "status": "cancelled",
                            "error": "Rejected by delegated approval" if delegated else "Rejected by local owner",
                        },
                    )
                elif receipt.get("approval") is True:
                    if not current or current != receipt.get("approved_template"):
                        raise ValueError("Template changed after local approval; submit a new request")
                    self.configure(binding)
                    receipt["phase"] = "launching"
                    write_json(path, receipt)
                    outcome = self.executor(
                        {
                            "action": "execution_start",
                            "project": binding["backend_project"],
                            "execution": local_id,
                            "template": request["template"],
                            **(
                                {"expected_execution_digest": receipt["delegated_execution_digest"]}
                                if receipt.get("delegated_execution_digest")
                                else {}
                            ),
                        }
                    )
                    if outcome.get("status") == "failed":
                        receipt.update(phase="terminal", result={"status": "failed", "error": outcome.get("error")})
                    else:
                        receipt["phase"] = "running"
            else:
                # launching means a process may have been created before a crash: reconcile its fixed ID.
                if not execution and not cancelled:
                    self._service_project_tools(receipt, cancelled)
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
                if not execution and receipt["phase"] == "terminal":
                    self._finalize_project_delivery(receipt, cancelled)
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
        # Running task tools need prompt polls. A long poll would otherwise
        # leave a model waiting for its Hub result for the full poll interval.
        active_tools = any(
            (row := read_json(path, {})).get("phase") == "running"
            and row.get("project_tools")
            and (row.get("job") or {}).get("kind") == "task"
            for path in (self.root / "pull-receipts").glob("*.json")
        )
        health = self._health()
        data = self.transport(
            self.config,
            "poll",
            {
                "bindings": self.advertisements(),
                "wait_seconds": 0 if active_tools else wait_seconds,
                "health": health,
                "encrypt_public_key": self.encryption_public_key(),
                "transfer_public_key": self.transfer_public_key(),
            },
        )
        ensure_hub_compatible(data)
        write_json(self.root / "broker-projects.json", data.get("projects", []))
        self._consume_transfers(data.get("transfers") or [])
        if data.get("owner_commands"):
            from agent_harness.owner_worker import OwnerControl

            if not self.config.get("owner_account_id"):
                raise PermissionError("This receiver has no enrolled account identity; pair it again")
            for envelope in data["owner_commands"]:
                result = OwnerControl(self).apply(envelope)
                self.transport(self.config, "owner-report", dict(result, id=envelope["body"]["id"]))
        cancellations = set(data.get("cancellations", []))
        attachment_decisions = {
            item.get("job_id"): item for item in data.get("attachment_decisions", []) if isinstance(item, dict)
        }
        execution_decisions = {
            item.get("job_id"): item for item in data.get("execution_decisions", []) if isinstance(item, dict)
        }
        features = data.get("features") or []
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
                if job_id in cancellations and (
                    receipt["phase"] == "waiting_attachment_approval"
                    or (
                        receipt["phase"] == "terminal"
                        and receipt.get("staged_file_delivery")
                        and receipt.get("result", {}).get("status") == "needs_review"
                    )
                ):
                    receipt.update(
                        phase="terminal",
                        result={"status": "cancelled", "error": "Cancelled before attachment delivery"},
                    )
                    write_json(path, receipt)
                if receipt["phase"] != "waiting_attachment_approval":
                    receipt = self.advance(
                        receipt,
                        job_id in cancellations,
                        execution_decisions.get(job_id),
                        project_tools="project_action_tools_v1" in features,
                        project_file_delivery="project_file_delivery_v1" in features,
                    )
                if receipt["phase"] in {"terminal", "waiting_attachment_approval"}:
                    from agent_harness.agent_attachments import prepare_reply

                    decision = attachment_decisions.get(job_id)
                    prepare_reply(self, receipt, features, decision)
                if receipt["phase"] == "waiting_attachment_approval":
                    continue
                result = receipt.get("result", {"status": "running", "local_status": receipt["phase"]})
                if receipt["phase"] == "waiting_local_approval" and receipt.get("execution_approval_digest"):
                    result["execution_approval_digest"] = receipt["execution_approval_digest"]
                if "stdout_tail" in result:
                    result["stdout"] = result.pop("stdout_tail")
                if "stderr_tail" in result:
                    result["stderr"] = result.pop("stderr_tail")
                status = result.get("status", "running")
                unknown_task = job["kind"] == "task" and status == "unknown"
                self.transport(
                    self.config,
                    "report",
                    {
                        "job_id": job_id,
                        "lease_id": job["lease_id"],
                        "state": status if receipt["phase"] == "terminal" or unknown_task else "running",
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
        components.append(self._executor_health())
        return report(components)

    def _executor_health(self):
        """Report the CLIs this machine is actually set up to run.

        The old check looked for a manually saved path, so a machine that found
        its CLI automatically reported "not configured" forever, and it always
        named Codex even when the model table only lists DeepSeek Harness.
        """
        from agent_harness import executors

        record = self.root / "executor.json"
        try:
            stamp = record.stat().st_mtime
        except OSError:
            stamp = 0.0
        cached = getattr(self, "_executor_health_cache", None)
        if cached and cached[0] == stamp and time.monotonic() - cached[1] < 120:
            return cached[2]
        labels = {"codex": "Codex", "dsh": "DeepSeek Harness"}
        try:
            kinds = sorted({item["executor"] for item in executors.models(self.root)})
        except (OSError, ValueError):
            kinds = [executors.CODEX]
        found, missing = [], []
        for kind in kinds:
            try:
                state = executors.status_of(self.root, kind)
            except (OSError, ValueError):
                state = {"found": False}
            (found if state.get("found") else missing).append(labels.get(kind, kind))
        if missing:
            entry = component(
                "executor",
                "degraded",
                HealthCode.CLI_NOT_FOUND,
                "未找到 " + "、".join(missing) + " CLI",
            )
        else:
            entry = component("executor", "healthy", HealthCode.OK, "已找到 " + "、".join(found))
        self._executor_health_cache = (stamp, time.monotonic(), entry)
        return entry


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
