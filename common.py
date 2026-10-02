"""Shared, dependency-free persistence and validation helpers."""
import contextlib
import json
import os
import re
import time
import uuid
from pathlib import Path


def identifier(value):
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", value or ""):
        raise ValueError("Invalid identifier; use 1-64 letters, digits, underscores or hyphens")
    return value


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return default


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        for attempt in range(20):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(.05)
    finally:
        tmp.unlink(missing_ok=True)


def validate_local_proxy(value):
    """Validate the app-managed loopback proxy setting."""
    if not isinstance(value, dict) or set(value) != {"enabled", "port"}:
        raise ValueError("Invalid local proxy setting")
    enabled, port = value["enabled"], value["port"]
    if not isinstance(enabled, bool):
        raise ValueError("Proxy switch must be on or off")
    if port is not None and (isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535):
        raise ValueError("Proxy port must be between 1 and 65535")
    if enabled and port is None:
        raise ValueError("Enter a local proxy port")
    return {"enabled": enabled, "port": port}


def apply_local_proxy(env, setting):
    """Apply an explicit app setting to one Codex child process environment."""
    setting = validate_local_proxy(setting)
    result = dict(env)
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "WS_PROXY", "WSS_PROXY"):
        result.pop(key, None)
        result.pop(key.lower(), None)
    if setting["enabled"]:
        address = f"http://127.0.0.1:{setting['port']}"
        result.update(HTTP_PROXY=address, HTTPS_PROXY=address,
                      NO_PROXY="localhost,127.0.0.1,::1")
    return result


@contextlib.contextmanager
def lock(path):
    """OS-backed lock; a process crash releases it automatically."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            # LK_NBLCK avoids the implicit ten-second timeout of LK_LOCK.
            deadline = time.monotonic() + 30
            while True:
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("State lock busy")
                    time.sleep(.05)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def tail(path, limit=65536):
    try:
        with Path(path).open("rb") as stream:
            stream.seek(0, 2)
            length = stream.tell()
            stream.seek(max(0, length - limit))
            return stream.read().decode("utf-8", errors="replace")
    except FileNotFoundError:
        return ""


def events(text):
    result = []
    for line in text.splitlines():
        try:
            value = json.loads(line)
            if isinstance(value, dict) and isinstance(value.get("type"), str):
                result.append(value)
        except (ValueError, TypeError):
            pass
    return result


def outcome(exit_code, event_list, cancelled=False, timed_out=False):
    if cancelled:
        return "cancelled"
    if timed_out:
        return "timed_out"
    types = {e["type"] for e in event_list}
    # Codex may emit a recoverable transport error, switch from WebSocket to
    # HTTPS, and still complete the turn. Final completion and process exit are
    # authoritative; worker.run separately requires a final report.
    if exit_code == 0 and "turn.completed" in types and "turn.failed" not in types:
        return "needs_review"
    return "failed"


def validate_project(data):
    identifier(data["name"])
    if not isinstance(data.get("model"), str) or not re.fullmatch(r"[a-zA-Z0-9_.-]+", data["model"]):
        raise ValueError("An explicit model is required")
    if data.get("effort") not in {"low", "medium", "high", "xhigh", "max", "ultra"}:
        raise ValueError("Invalid reasoning effort")
    path = Path(data["remote_path"])
    if not path.is_absolute() or path == Path(path.anchor):
        raise ValueError("Remote project must be an absolute directory, not a drive root")
    if not path.is_dir():
        raise ValueError("Remote project directory does not exist")
    data["remote_path"] = str(path.resolve())
    timeout = data.get("timeout", 1800)
    if not isinstance(timeout, int) or not 30 <= timeout <= 86400:
        raise ValueError("Timeout must be between 30 and 86400 seconds")
    return data
