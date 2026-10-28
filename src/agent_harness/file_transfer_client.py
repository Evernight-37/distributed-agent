"""Persistent native client for account/device file transfers."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from agent_harness.attachments import safe_name
from agent_harness.broker_transport import broker_opener
from agent_harness.common import lock, read_json, write_json
from agent_harness.file_transfers import CHUNK_BYTES, _open_regular, _regular_size
from agent_harness.pull_worker import validate_server

TERMINAL = {"completed", "rejected", "cancelled", "expired"}


def file_sha256(path, progress=None):
    digest = hashlib.sha256()
    completed = 0
    with _open_regular(Path(path)) as stream:
        for chunk in iter(lambda: stream.read(CHUNK_BYTES), b""):
            digest.update(chunk)
            completed += len(chunk)
            if progress:
                progress("hashing", completed)
    return digest.hexdigest()


def _http_error(exc, fallback):
    try:
        return json.load(exc).get("error", fallback)
    except Exception:
        return fallback


def upload(bridge, server, transfer_id, source, expected_sha256, progress=None):
    source = Path(source).resolve()
    try:
        size = _regular_size(source)
    except (FileNotFoundError, RuntimeError):
        raise ValueError("待发送文件不存在或不是普通文件") from None
    if size <= 0:
        raise ValueError("不能发送空文件")
    if progress:
        progress("verifying_source", 0, size)
    if file_sha256(source, progress) != expected_sha256:
        raise RuntimeError("待发送文件在请求确认后发生了变化，请取消并重新发送")
    session = bridge._rpc("transfer-upload-start", {"id": transfer_id}, server=server, timeout=45)
    received = session["received_bytes"]
    if not isinstance(received, int) or not 0 <= received <= size:
        raise RuntimeError("协作服务返回了无效上传位置")
    validate_server(server)
    opener = broker_opener(bridge.broker_proxy)
    with _open_regular(source) as stream:
        stream.seek(received)
        if progress:
            progress("uploading", received, size)
        while received < size:
            content = stream.read(min(session.get("chunk_bytes", CHUNK_BYTES), size - received))
            if not content:
                raise RuntimeError("读取待发送文件时意外结束")
            end = received + len(content) - 1
            request = urllib.request.Request(
                server.rstrip("/") + "/api/transfers/uploads/" + transfer_id,
                data=content,
                method="PUT",
                headers={
                    "Authorization": "Bearer " + session["upload_token"],
                    "Content-Type": "application/octet-stream",
                    "Content-Range": f"bytes {received}-{end}/{size}",
                    "X-Chunk-SHA256": hashlib.sha256(content).hexdigest(),
                },
            )
            try:
                with opener.open(request, timeout=90) as response:
                    result = json.load(response)
            except urllib.error.HTTPError as exc:
                raise RuntimeError(_http_error(exc, "文件分块上传失败")) from None
            except urllib.error.URLError:
                raise RuntimeError("发送中断；联网后可继续发送") from None
            received = result.get("received_bytes")
            if received != end + 1:
                raise RuntimeError("协作服务确认的上传位置不一致")
            if progress:
                progress("uploading", received, size)
    if progress:
        progress("verifying", size, size)
    return bridge._rpc("transfer-upload-finish", {"id": transfer_id}, server=server, timeout=180)


def download(bridge, server, transfer_id, destination, progress=None):
    ticket = bridge._rpc("transfer-download-ticket", {"id": transfer_id}, server=server, timeout=30)
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if (
            _regular_size(destination, missing_ok=True) == ticket["size"]
            and file_sha256(destination) == ticket["sha256"]
        ):
            bridge._rpc("transfer-complete", {"id": transfer_id}, server=server, timeout=30)
            return {"saved": True, "name": destination.name, "size": ticket["size"]}
        raise FileExistsError("保存位置已有同名文件，请重新选择")
    free = shutil.disk_usage(destination.parent).free
    if ticket["size"] + max(256 * 1024 * 1024, ticket["size"] // 50) > free:
        raise RuntimeError("目标磁盘空间不足")
    try:
        _regular_size(destination, missing_ok=True)
    except RuntimeError:
        raise ValueError("保存位置必须是普通文件") from None
    temporary = destination.with_name(destination.name + "." + transfer_id + ".part")
    try:
        received = _regular_size(temporary, missing_ok=True) or 0
    except RuntimeError:
        raise ValueError("下载暂存位置必须是普通文件") from None
    if received > ticket["size"]:
        temporary.unlink()
        received = 0
    validate_server(server)
    opener = broker_opener(bridge.broker_proxy)
    if progress:
        progress("downloading", received, ticket["size"])
    with _open_regular(temporary, writable=True) as stream:
        stream.truncate(received)
        stream.seek(received)
        while received < ticket["size"]:
            end = min(received + CHUNK_BYTES, ticket["size"]) - 1
            request = urllib.request.Request(
                server.rstrip("/") + "/api/transfers/" + transfer_id + "/content",
                headers={
                    "Authorization": "Bearer " + ticket["download_token"],
                    "Range": f"bytes={received}-{end}",
                },
            )
            try:
                with opener.open(request, timeout=90) as response:
                    content = response.read(CHUNK_BYTES + 1)
            except urllib.error.HTTPError as exc:
                raise RuntimeError(_http_error(exc, "文件分块下载失败")) from None
            except urllib.error.URLError:
                raise RuntimeError("接收中断；联网后可继续接收") from None
            if len(content) != end - received + 1:
                raise RuntimeError("文件下载分块不完整")
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            received += len(content)
            if progress:
                progress("downloading", received, ticket["size"])
    if progress:
        progress("verifying", 0, ticket["size"])
    digest = file_sha256(
        temporary,
        lambda _phase, done: progress("verifying", done, ticket["size"]) if progress else None,
    )
    if digest != ticket["sha256"]:
        raise RuntimeError("接收文件 SHA-256 校验失败；暂存文件已保留")
    os.replace(temporary, destination)
    bridge._rpc("transfer-complete", {"id": transfer_id}, server=server, timeout=30)
    return {"saved": True, "name": destination.name, "size": ticket["size"]}


class FileTransferManager:
    """Keep local source/destination manifests and resume work after restarts."""

    def __init__(self, bridge):
        self.bridge = bridge
        self.path = bridge.root / "file-transfer-jobs.json"
        self.settings_path = bridge.root / "file-transfer-settings.json"
        self.guard = threading.RLock()
        self.running = set()

    def _document(self):
        value = read_json(self.path, {"version": 1, "jobs": {}})
        if not isinstance(value, dict) or not isinstance(value.get("jobs"), dict):
            return {"version": 1, "jobs": {}}
        value["version"] = 1
        return value

    def _save_job(self, job):
        with lock(self.path.with_suffix(".lock")):
            document = self._document()
            document["jobs"][job["id"]] = job
            finished = sorted(
                (
                    item
                    for item in document["jobs"].values()
                    if item.get("state") in {"completed", "rejected", "cancelled", "expired"}
                ),
                key=lambda item: item.get("updated_at", 0),
            )
            for stale in finished[:-100]:
                document["jobs"].pop(stale["id"], None)
            write_json(self.path, document)

    def _job(self, job_id):
        return self._document()["jobs"].get(job_id)

    def settings(self):
        value = read_json(self.settings_path, {})
        folder = value.get("receive_folder") if isinstance(value, dict) else None
        return {"receive_folder_configured": bool(folder and Path(folder).is_dir())}

    def set_receive_folder(self, folder):
        folder = Path(folder).resolve()
        if not folder.is_dir() or folder == Path(folder.anchor):
            raise ValueError("请选择现有文件夹，不能使用磁盘根目录")
        write_json(self.settings_path, {"receive_folder": str(folder)})
        return self.settings()

    def _receive_folder(self):
        folder = read_json(self.settings_path, {}).get("receive_folder")
        return Path(folder).resolve() if folder and Path(folder).is_dir() else None

    @staticmethod
    def _public(job):
        hidden = {"source", "destination", "sha256", "server", "created_at"}
        return {key: value for key, value in job.items() if key not in hidden}

    def jobs(self):
        return [self._public(dict(job)) for job in self._document()["jobs"].values()]

    def _update(self, job_id, **values):
        with self.guard:
            job = self._job(job_id)
            if not job:
                return
            job.update(values, updated_at=time.time())
            self._save_job(job)

    def _progress(self, job_id):
        def report(phase, completed, total=None):
            values = {"phase": phase, "completed_bytes": completed, "state": "running"}
            if total is not None:
                values["total_bytes"] = total
            self._update(job_id, **values)

        return report

    def _launch(self, job_id, target):
        with self.guard:
            if job_id in self.running:
                return
            self.running.add(job_id)

        def run():
            try:
                result = target(self._progress(job_id))
                if isinstance(result, dict) and result.pop("_local_wait", False):
                    self._update(job_id, state="waiting", result=result, error=None)
                else:
                    self._update(job_id, state="completed", phase="completed", result=result, error=None)
            except Exception as exc:
                self._update(job_id, state="paused", phase="paused", error=str(exc))
            finally:
                with self.guard:
                    self.running.discard(job_id)

        threading.Thread(target=run, name="file-transfer", daemon=True).start()

    def _prepare_outgoing(self, job, progress):
        source = Path(job["source"])
        size = job["total_bytes"]
        digest = file_sha256(source, lambda phase, done: progress(phase, done, size))
        transfer = self.bridge._rpc(
            "transfer-request",
            {
                "recipient_id": job["recipient_id"],
                "recipient_node": job.get("recipient_node"),
                "name": source.name,
                "size": size,
                "sha256": digest,
            },
            server=job["server"],
            timeout=45,
        )
        self._update(
            job["id"],
            transfer_id=transfer["id"],
            sha256=digest,
            state="waiting",
            phase="waiting_recipient" if transfer["state"] == "requested" else "accepted",
        )
        if transfer["state"] == "accepted":
            return upload(self.bridge, job["server"], transfer["id"], source, digest, progress)
        return {"_local_wait": True, "requested": True, "transfer_id": transfer["id"]}

    def send(self, server, recipient_id, recipient_node, source):
        source = Path(source).resolve()
        try:
            size = _regular_size(source)
        except (FileNotFoundError, RuntimeError):
            raise ValueError("请选择普通文件") from None
        if size <= 0:
            raise ValueError("不能发送空文件")
        job_id = "local-transfer-" + secrets.token_hex(16)
        job = {
            "id": job_id,
            "direction": "outgoing",
            "server": server,
            "recipient_id": recipient_id,
            "recipient_node": recipient_node,
            "name": source.name,
            "source": str(source),
            "total_bytes": size,
            "completed_bytes": 0,
            "state": "running",
            "phase": "hashing",
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        self._save_job(job)

        self._launch(job_id, lambda progress: self._prepare_outgoing(job, progress))
        return self.status(job_id)

    def accept(self, server, transfer, destination):
        destination = Path(destination).resolve()
        result = transfer
        if transfer["sender_id"] != transfer["recipient_id"]:
            result = self.bridge._rpc(
                "transfer-decide", {"id": transfer["id"], "accept": True}, server=server, timeout=30
            )
        job_id = "local-transfer-" + secrets.token_hex(16)
        job = {
            "id": job_id,
            "transfer_id": transfer["id"],
            "direction": "incoming",
            "server": server,
            "name": transfer["name"],
            "destination": str(destination),
            "total_bytes": transfer["size"],
            "completed_bytes": 0,
            "state": "waiting",
            "phase": "waiting_sender",
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        self._save_job(job)
        if result["state"] == "ready":
            self._launch(
                job_id,
                lambda progress: download(self.bridge, server, transfer["id"], destination, progress),
            )
        return self.status(job_id)

    def reconcile(self, remote_transfers):
        """Start only resumable work; never hold a session waiting for a peer."""

        remote = {(item["server"], item["id"]): item for item in remote_transfers}
        document = self._document()
        known = {(job.get("server"), job.get("transfer_id")) for job in document["jobs"].values()}
        folder = self._receive_folder()
        account_id = self.bridge.identity.get("account_id") if self.bridge.identity else None
        node = self.bridge.profile.get("node") if self.bridge.profile else None
        for item in remote_transfers:
            key = (item["server"], item["id"])
            if (
                key not in known
                and folder
                and item.get("sender_id") == account_id
                and item.get("recipient_id") == account_id
                and item.get("recipient_node") == node
            ):
                destination = folder / safe_name(item["name"])
                if destination.exists():
                    stem, suffix = destination.stem, destination.suffix
                    index = 2
                    while destination.exists():
                        destination = folder / f"{stem} ({index}){suffix}"
                        index += 1
                job = {
                    "id": "local-transfer-" + secrets.token_hex(16),
                    "transfer_id": item["id"],
                    "direction": "incoming",
                    "server": item["server"],
                    "name": item["name"],
                    "destination": str(destination),
                    "total_bytes": item["size"],
                    "completed_bytes": 0,
                    "state": "waiting",
                    "phase": "waiting_sender",
                    "created_at": time.time(),
                    "updated_at": time.time(),
                }
                self._save_job(job)
                document["jobs"][job["id"]] = job
                known.add(key)
        for job in list(document["jobs"].values()):
            transfer_id = job.get("transfer_id")
            if job.get("state") == "completed":
                continue
            if job.get("state") == "paused" and time.time() - float(job.get("updated_at", 0)) < 15:
                continue
            if not transfer_id:
                if job.get("direction") == "outgoing":
                    self._launch(
                        job["id"],
                        lambda progress, j=dict(job): self._prepare_outgoing(j, progress),
                    )
                continue
            item = remote.get((job.get("server"), transfer_id))
            if not item:
                continue
            if item["state"] in TERMINAL:
                self._update(job["id"], state=item["state"], phase=item["state"])
            elif job["direction"] == "outgoing" and item["state"] in {"accepted", "uploading"}:
                self._launch(
                    job["id"],
                    lambda progress, j=dict(job), i=dict(item): upload(
                        self.bridge, j["server"], i["id"], j["source"], j["sha256"], progress
                    ),
                )
            elif job["direction"] == "incoming" and item["state"] == "ready":
                self._launch(
                    job["id"],
                    lambda progress, j=dict(job), i=dict(item): download(
                        self.bridge, j["server"], i["id"], j["destination"], progress
                    ),
                )
            else:
                phase = "waiting_recipient" if item["state"] == "requested" else "waiting_sender"
                self._update(job["id"], state="waiting", phase=phase, error=None)

    def status(self, job_id):
        job = self._job(job_id)
        if not job:
            raise ValueError("本机文件传输记录不存在")
        return self._public(dict(job))
