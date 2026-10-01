"""Serialized deployment with atomic file replacement and the same lock as job starts."""
import base64
import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path


def deploy(root, payload):
    root = Path(root).resolve()
    if not root.is_absolute() or root == Path(root.anchor):
        raise ValueError("Unsafe deployment directory")
    if set(payload) not in ({"common.py", "worker.py"}, {"common.py", "worker.py", "jobrunner.py"}):
        raise ValueError("Unexpected deployment files")
    files = {name: base64.b64decode(value, validate=True) for name, value in payload.items()}
    for name, content in files.items():
        compile(content.decode("utf-8"), name, "exec")
    root.mkdir(parents=True, exist_ok=True)
    with (root / "registry.lock").open("a+b") as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            deadline = time.monotonic() + 30
            while True:
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Deployment lock busy")
                    time.sleep(.05)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            for active in (root / "projects").glob("*/active.json"):
                if json.loads(active.read_text(encoding="utf-8-sig")) is not None:
                    raise ValueError("Refusing deployment with active or unreconciled jobs; query status first")
            for state in (root / 'executions').glob('*/state.json'):
                if json.loads(state.read_text(encoding='utf-8-sig')).get('status') in {'starting','running','unknown'}:
                    raise ValueError('Refusing deployment while background execution is active or unreconciled')
            for name, content in files.items():
                tmp = root / (name + "." + uuid.uuid4().hex + ".tmp")
                try:
                    tmp.write_bytes(content)
                    os.replace(tmp, root / name)
                finally:
                    tmp.unlink(missing_ok=True)
            return {"deployed": True, "sha256": {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}}
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


if __name__ == "__main__":
    try:
        bundled = globals().get("_deploy_payload")
        payload = bundled["files"] if bundled is not None else json.loads(sys.stdin.buffer.read().decode("utf-8"))
        result = deploy(sys.argv[1], payload)
        print(json.dumps({"ok": True, "result": result}))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        sys.exit(1)
