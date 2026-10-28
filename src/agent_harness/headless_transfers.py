"""Background file delivery for an enrolled headless receiver."""

from __future__ import annotations

import secrets
import threading
import time
from pathlib import Path

from agent_harness.attachments import safe_name
from agent_harness.common import lock, read_json, write_json
from agent_harness.file_transfer_client import download


class _DeviceBridge:
    """Adapt the receiver's bearer-authenticated device API to the downloader."""

    def __init__(self, connector):
        self.connector = connector
        self.broker_proxy = connector.config.get("broker_proxy")

    def _rpc(self, operation, payload, server=None, timeout=None):
        del server, timeout
        mapped = {
            "transfer-download-ticket": "transfer-download-ticket",
            "transfer-complete": "transfer-complete",
        }.get(operation)
        if not mapped:
            raise ValueError("Unsupported headless transfer operation")
        return self.connector.transport(self.connector.config, mapped, payload)


class HeadlessTransferReceiver:
    """Resume assigned downloads without blocking task and command polling."""

    def __init__(self, connector):
        self.connector = connector
        self.root = Path(connector.root).resolve()
        self.path = self.root / "headless-file-transfers.json"
        self.guard = threading.RLock()
        self.running = set()

    def _document(self):
        value = read_json(self.path, {"version": 1, "jobs": {}})
        if not isinstance(value, dict) or not isinstance(value.get("jobs"), dict):
            return {"version": 1, "jobs": {}}
        return value

    def _update(self, transfer_id, **changes):
        with lock(self.root / "headless-file-transfers.lock"):
            document = self._document()
            job = document["jobs"].setdefault(transfer_id, {"id": transfer_id})
            job.update(changes, updated_at=time.time())
            write_json(self.path, document)

    def _receive_folder(self):
        settings = read_json(self.root / "file-transfer-settings.json", {})
        saved = settings.get("receive_folder") if isinstance(settings, dict) else None
        folder = Path(saved).resolve() if isinstance(saved, str) else (self.root / "received-files").resolve()
        if not folder.is_absolute() or folder == Path(folder.anchor) or not folder.is_dir():
            if saved:
                raise ValueError("配置的接收目录不存在，请由账户所有者重新设置")
            folder.mkdir(parents=True, exist_ok=True)
        try:
            folder.chmod(0o700)
        except OSError:
            pass
        return folder

    def _destination(self, transfer):
        with lock(self.root / "headless-file-transfers.lock"):
            document = self._document()
            job = document["jobs"].get(transfer["id"], {})
            saved = job.get("destination")
            if isinstance(saved, str):
                destination = Path(saved)
                if (
                    destination.is_absolute()
                    and destination.parent != Path(destination.anchor)
                    and destination.parent.is_dir()
                ):
                    return destination
            folder = self._receive_folder()
            destination = folder / safe_name(transfer["name"])
            stem, suffix = destination.stem, destination.suffix
            index = 2
            reserved = {item.get("destination") for item in document["jobs"].values() if isinstance(item, dict)}
            while destination.exists() or str(destination) in reserved:
                destination = folder / f"{stem} ({index}){suffix}"
                index += 1
            document["jobs"][transfer["id"]] = {
                "id": transfer["id"],
                "name": transfer["name"],
                "destination": str(destination),
                "state": "waiting",
                "created_at": time.time(),
                "updated_at": time.time(),
            }
            write_json(self.path, document)
            return destination

    def _run(self, transfer):
        transfer_id = transfer["id"]
        try:
            destination = self._destination(transfer)
            self._update(transfer_id, state="downloading", error=None)
            result = download(
                _DeviceBridge(self.connector),
                self.connector.config["server"],
                transfer_id,
                destination,
            )
            self._update(transfer_id, state="completed", result=result, error=None)
        except Exception as exc:
            self._update(transfer_id, state="paused", error=str(exc)[:1000])
        finally:
            with self.guard:
                self.running.discard(transfer_id)

    def reconcile(self):
        """Poll assigned transfers and start at most three independent downloads."""

        result = self.connector.transport(self.connector.config, "transfers", {})
        transfers = result.get("transfers", []) if isinstance(result, dict) else []
        document = self._document()
        for transfer in transfers:
            if not isinstance(transfer, dict) or transfer.get("state") != "ready":
                continue
            transfer_id = transfer.get("id")
            if not isinstance(transfer_id, str):
                continue
            job = document["jobs"].get(transfer_id, {})
            if job.get("state") == "completed":
                continue
            with self.guard:
                if transfer_id in self.running or len(self.running) >= 3:
                    continue
                self.running.add(transfer_id)
            threading.Thread(
                target=self._run,
                args=(dict(transfer),),
                name="headless-file-transfer-" + secrets.token_hex(4),
                daemon=True,
            ).start()
        return {"assigned": len(transfers), "running": len(self.running)}
