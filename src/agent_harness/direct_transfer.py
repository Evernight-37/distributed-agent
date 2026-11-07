"""Device-to-device file transfer over the tailnet.

The collaboration service stays out of the data path: it only tells the receiver
where the sender's endpoint is. Tailscale's WireGuard link already encrypts the
bytes, so this module adds the two things the transport does not give us:
authentication (only the intended recipient may fetch) and resumption (a
multi-gigabyte transfer must survive a dropped connection).

Authentication is a device-key signature over the request line and a timestamp.
Against another member of the same project this is a real boundary; against the
service operator it is not, because the operator publishes the device keys used
to verify. That matches the project's stated trust model.
"""

import base64
import hashlib
import http.server
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

CHUNK = 1024 * 1024
SKEW_SECONDS = 300


def _b64(raw):
    return base64.b64encode(raw).decode("ascii")


def _unb64(value, length=None):
    try:
        raw = base64.b64decode(str(value).encode("ascii"), validate=True)
    except Exception:
        raise ValueError("Invalid signature material") from None
    if length is not None and len(raw) != length:
        raise ValueError("Invalid signature material")
    return raw


def local_tailnet_address():
    """The address peers reach this machine on, or None when Tailscale is absent.

    A transfer must not silently fall back to staging bytes at the service, so a
    caller that cannot find one has to fail instead of degrading.
    """
    explicit = os.environ.get("TAILSCALE_IP", "").strip()
    if explicit:
        return explicit
    executable = shutil.which("tailscale.exe" if os.name == "nt" else "tailscale")
    if not executable:
        return None
    try:
        result = subprocess.run(
            [executable, "ip", "-4"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.split():
        candidate = line.strip()
        if candidate and candidate.count(".") == 3:
            return candidate
    return None


def canonical_request(transfer_id, stamp, resource):
    """What the recipient signs: this exact request, once, within the skew window."""
    return ("harness-transfer-v1\n%s\n%s\n%s" % (transfer_id, int(stamp), resource)).encode("utf-8")


class TransferError(RuntimeError):
    pass


class TransferPending(TransferError):
    """The sender has not been told which device claimed the transfer yet.

    Not a refusal: nobody may fetch until the signing key of the claiming device
    reaches the sender, so the receiver retries shortly instead of failing.
    """


def _digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_marker(path):
    try:
        value = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_marker(path, transfer_id, size, mtime, digest):
    path.write_text(
        json.dumps({"transfer_id": transfer_id, "size": size, "mtime": mtime, "digest": digest}),
        "utf-8",
    )


def _changed(recorded, size, mtime, digest):
    """Whether a partial file may be continued against the sender's current file."""
    if not recorded:
        return True
    if size is not None and recorded.get("size") != size:
        return True
    if mtime is not None and recorded.get("mtime") != mtime:
        return True
    if digest is not None and recorded.get("digest") != digest:
        return True
    return False


class Sender:
    """Serves one file to one recipient, by range, on an ephemeral port."""

    def __init__(self, path, transfer_id, recipient_public_key, host):
        self.path = Path(path).resolve()
        if not self.path.is_file() or self.path.is_symlink():
            raise ValueError("仅支持普通文件")
        self.transfer_id = transfer_id
        self.recipient_public_key = recipient_public_key
        self.host = host
        self.size = self.path.stat().st_size
        self.sha256 = None
        self.httpd = None
        self.thread = None
        self._digest_thread = None

    def digest_in_background(self):
        """Hash without blocking the transfer; a large file must not delay serving."""
        if self._digest_thread is not None:
            return
        sender = self

        def run():
            try:
                sender.sha256 = _digest(sender.path)
            except OSError:
                sender.sha256 = None

        self._digest_thread = threading.Thread(target=run, daemon=True)
        self._digest_thread.start()

    def set_recipient(self, public_key):
        """Learn the key of the device that claimed this transfer."""
        self.recipient_public_key = public_key

    def _authorized(self, resource, header):
        try:
            public, stamp, signature = str(header or "").split(".")
        except ValueError:
            return False
        if public != self.recipient_public_key:
            return False
        try:
            stamp_value = int(stamp)
        except ValueError:
            return False
        if abs(time.time() - stamp_value) > SKEW_SECONDS:
            return False
        message = canonical_request(self.transfer_id, stamp_value, resource)
        try:
            Ed25519PublicKey.from_public_bytes(_unb64(public, 32)).verify(_unb64(signature, 64), message)
        except Exception:
            return False
        return True

    def _handler(self):
        sender = self
        meta_resource = "/transfer/" + sender.transfer_id + "/meta"
        file_resource = "/transfer/" + sender.transfer_id

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                return

            def _deny(self, code=403):
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def _gate(self, resource):
                """409 while nobody has claimed the transfer, 403 when the claim is wrong."""
                if not sender.recipient_public_key:
                    self._deny(409)
                    return False
                if not sender._authorized(resource, self.headers.get("X-Harness-Auth", "")):
                    self._deny(403)
                    return False
                return True

            def do_HEAD(self):
                if not self._gate(meta_resource):
                    return
                self.send_response(200)
                self.send_header("X-Harness-Size", str(sender.size))
                self.send_header("X-Harness-Name", urllib.parse.quote(sender.path.name))
                try:
                    self.send_header("X-Harness-Mtime", str(sender.path.stat().st_mtime_ns))
                except OSError:
                    pass
                if sender.sha256:
                    self.send_header("X-Harness-Sha256", sender.sha256)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                if not self._gate(file_resource):
                    return
                start = 0
                header = self.headers.get("Range", "")
                if header.startswith("bytes="):
                    try:
                        start = int(header[6:].split("-")[0] or 0)
                    except ValueError:
                        return self._deny(416)
                if start >= sender.size:
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % sender.size)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(206 if start else 200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(sender.size - start))
                self.send_header("Accept-Ranges", "bytes")
                if start:
                    self.send_header("Content-Range", "bytes %d-%d/%d" % (start, sender.size - 1, sender.size))
                self.end_headers()
                with sender.path.open("rb") as stream:
                    stream.seek(start)
                    for block in iter(lambda: stream.read(CHUNK), b""):
                        try:
                            self.wfile.write(block)
                        except (BrokenPipeError, ConnectionResetError):
                            return

        return Handler

    def start(self):
        self.httpd = http.server.ThreadingHTTPServer((self.host, 0), self._handler())
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.digest_in_background()
        return self.httpd.server_address[1]

    def stop(self):
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None


def authorization(private_key, public_key, transfer_id, resource, stamp=None):
    """The header the recipient sends; a third party cannot forge it."""
    stamp = int(time.time() if stamp is None else stamp)
    message = canonical_request(transfer_id, stamp, resource)
    signature = Ed25519PrivateKey.from_private_bytes(_unb64(private_key, 32)).sign(message)
    return "%s.%d.%s" % (public_key, stamp, _b64(signature))


def probe(url, private_key, public_key, transfer_id, timeout=10):
    """Connectivity and metadata check both sides run before moving bytes."""
    resource = "/transfer/" + transfer_id + "/meta"
    request = urllib.request.Request(
        url,
        method="HEAD",
        headers={"X-Harness-Auth": authorization(private_key, public_key, transfer_id, resource)},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            mtime = response.headers.get("X-Harness-Mtime")
            return {
                "size": int(response.headers.get("X-Harness-Size", "0")),
                "name": urllib.parse.unquote(response.headers.get("X-Harness-Name", "")),
                "sha256": response.headers.get("X-Harness-Sha256"),
                "mtime": int(mtime) if mtime and mtime.isdigit() else None,
            }
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            raise TransferPending("对方尚未确认接收设备") from None
        raise TransferError("对方拒绝连接：%s" % exc.code) from None
    except OSError as exc:
        raise TransferError("对方不可达：%s" % exc) from None


def pull(
    url,
    private_key,
    public_key,
    transfer_id,
    destination,
    size=None,
    attempts=12,
    pause=1.0,
    progress=None,
):
    """Download, waiting while the sender has not learned this device's key.

    Until the claiming device's key reaches the sender it answers 409 to
    everybody; that is a wait, not a refusal, so keep trying instead of failing
    the whole transfer. Every other error propagates at once.
    """
    last = None
    for _attempt in range(attempts):
        try:
            meta = probe(url, private_key, public_key, transfer_id)
            fetch(
                url,
                private_key,
                public_key,
                transfer_id,
                destination,
                expect_size=meta.get("size") or size,
                expect_digest=meta.get("sha256"),
                expect_mtime=meta.get("mtime"),
                progress=progress,
            )
            return meta
        except TransferPending as exc:
            last = exc
            time.sleep(pause)
    raise last


def fetch(
    url,
    private_key,
    public_key,
    transfer_id,
    destination,
    expect_size=None,
    expect_digest=None,
    expect_mtime=None,
    progress=None,
):
    """Download with resumption. Returns the finished path; partial data stays put.

    A partial file is only continued when the sender advertises the same size,
    modification time and digest as when that partial file was started: splicing
    bytes from two versions of the source produces a right-sized, wrong file.
    """
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    part = destination.with_name(destination.name + ".part")
    marker = part.with_name(part.name + ".json")
    offset = part.stat().st_size if part.exists() else 0
    if offset and _changed(_read_marker(marker), expect_size, expect_mtime, expect_digest):
        part.unlink()
        marker.unlink(missing_ok=True)
        offset = 0
    resource = "/transfer/" + transfer_id
    headers = {"X-Harness-Auth": authorization(private_key, public_key, transfer_id, resource)}
    if offset:
        headers["Range"] = "bytes=%d-" % offset
    try:
        response = urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60)
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            raise TransferPending("对方尚未确认接收设备") from None
        raise TransferError("传输被拒绝：%s" % exc.code) from None
    except OSError as exc:
        raise TransferError("传输中断：%s" % exc) from None
    if offset and getattr(response, "status", None) != 206:
        # The sender ignored the range; appending a whole body would duplicate it.
        offset = 0
    try:
        if not offset:
            _write_marker(marker, transfer_id, expect_size, expect_mtime, expect_digest)
        with part.open("ab" if offset else "wb") as sink:
            for block in iter(lambda: response.read(CHUNK), b""):
                sink.write(block)
                if progress:
                    progress(sink.tell())
    finally:
        response.close()
    written = part.stat().st_size
    if expect_size is not None and written != expect_size:
        raise TransferError("传输不完整：%d/%d" % (written, expect_size))
    if expect_digest and _digest(part) != expect_digest:
        part.unlink()
        marker.unlink(missing_ok=True)
        raise TransferError("传输内容与发送方不一致（文件可能已改变），已丢弃，请重试")
    part.replace(destination)
    marker.unlink(missing_ok=True)
    return destination
