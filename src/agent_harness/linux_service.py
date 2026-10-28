"""Small, non-privileged manager for the Linux user receiver service.

The manager deliberately uses ``systemctl --user`` only.  It never enables
linger, writes outside the user's config directory, or removes receiver data.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


UNIT_NAME = "agent-harness-receiver.service"


def _path(value, label):
    value = Path(value).expanduser().resolve()
    if any(char in str(value) for char in "\r\n\x00"):
        raise ValueError(f"{label} contains a control character")
    return value


def _unit_quote(value, label):
    """Quote a systemd ExecStart token and reject unsafe control characters."""

    value = str(_path(value, label))
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ValueError(f"{label} contains a control character")
    # A percent in an ExecStart command is a systemd specifier.  Doubling it
    # preserves a literal percent in a path while avoiding interpolation.
    return '"' + value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"') + '"'


def unit_path(home=None):
    home = Path.home() if home is None else _path(home, "home")
    return home / ".config" / "systemd" / "user" / UNIT_NAME


def build_unit(python, script, root):
    """Return a complete user unit for the foreground receiver supervisor."""

    python = _unit_quote(python, "python")
    root = _unit_quote(root, "data root")
    command = (
        f"{python} --root {root} run"
        if script is None
        else f"{python} -X utf8 {_unit_quote(script, 'script')} --root {root} run"
    )
    return (
        "[Unit]\n"
        "Description=Agent Harness Linux receiver\n"
        "Wants=network-online.target\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={command}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "TimeoutStopSec=75\n"
        "UMask=0077\n"
        "Environment=PYTHONUNBUFFERED=1\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


class ServiceManager:
    """Manage the current user's receiver unit without touching its data."""

    def __init__(self, root, python, script, home=None):
        self.root = _path(root, "data root")
        self.python = _path(python, "python")
        self.script = _path(script, "script") if script is not None else None
        self.home = Path.home() if home is None else _path(home, "home")
        self.path = unit_path(self.home)

    @staticmethod
    def _command(*args, check=False):
        if shutil.which("systemctl") is None:
            raise RuntimeError("未找到 systemctl；Linux 接收端需要 systemd --user")
        result = subprocess.run(
            ["systemctl", "--user", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if check and result.returncode:
            message = (result.stderr or result.stdout or "systemctl 操作失败").strip()
            raise RuntimeError(message[:1000])
        return result

    def install(self):
        content = build_unit(self.python, self.script, self.root)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(content, encoding="utf-8", newline="\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)
        self._command("daemon-reload", check=True)
        self._command("enable", UNIT_NAME, check=True)
        # `enable --now` does not restart an already active service after its
        # unit changes. An explicit restart is required for upgrades.
        self._command("restart", UNIT_NAME, check=True)
        active = self._command("is-active", UNIT_NAME)
        if active.returncode:
            raise RuntimeError((active.stderr or active.stdout or "接收服务未能启动").strip()[:1000])
        return {"unit": str(self.path), "active": active.stdout.strip() == "active"}

    def start(self):
        return self._command("start", UNIT_NAME, check=True)

    def stop(self):
        return self._command("stop", UNIT_NAME, check=True)

    def restart(self):
        return self._command("restart", UNIT_NAME, check=True)

    def status(self):
        result = self._command("is-active", UNIT_NAME)
        return {
            "active": result.returncode == 0 and result.stdout.strip() == "active",
            "output": result.stdout.strip(),
        }

    def logs(self, lines=100, follow=False):
        if not isinstance(lines, int) or isinstance(lines, bool) or not 1 <= lines <= 10000:
            raise ValueError("日志行数必须在 1 到 10000 之间")
        args = ["--user", "-u", UNIT_NAME, "-n", str(lines), "--no-pager"]
        if follow:
            args.append("-f")
        if shutil.which("journalctl") is None:
            raise RuntimeError("未找到 journalctl")
        return subprocess.run(
            ["journalctl", *args],
            capture_output=not follow,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

    def receiver_logs(self, lines=100, byte_limit=262144):
        """Return bounded service stdout/stderr only; never task prompts or reports."""

        if not isinstance(lines, int) or isinstance(lines, bool) or not 1 <= lines <= 10000:
            raise ValueError("日志行数必须在 1 到 10000 之间")
        if not isinstance(byte_limit, int) or not 1024 <= byte_limit <= 1048576:
            raise ValueError("日志读取上限无效")
        receivers = self.root / "receivers"
        result = []
        for receiver in sorted(receivers.glob("device-*")):
            if not receiver.is_dir() or receiver.resolve().parent != receivers.resolve():
                continue
            item = {"receiver": receiver.name}
            for name, key in (
                ("desktop-service.stdout.log", "stdout"),
                ("desktop-service.stderr.log", "stderr"),
            ):
                path = receiver / name
                if path.is_symlink() or not path.is_file() or path.resolve().parent != receiver.resolve():
                    continue
                with path.open("rb") as stream:
                    stream.seek(0, 2)
                    stream.seek(max(0, stream.tell() - byte_limit))
                    text = stream.read(byte_limit).decode("utf-8", errors="replace")
                item[key] = "\n".join(text.splitlines()[-lines:])
            if len(item) > 1:
                result.append(item)
        return result

    def uninstall(self):
        # A missing unit is already uninstalled; tolerate this for idempotence.
        self._command("disable", "--now", UNIT_NAME, check=False)
        if self.path.exists():
            self.path.unlink()
        self._command("daemon-reload", check=True)
        return {"unit": str(self.path), "data_root": str(self.root), "removed": True}

    @staticmethod
    def linger_status():
        """Read linger state without changing it; unavailable is not an error."""

        if shutil.which("loginctl") is None:
            return {"available": False, "enabled": None}
        result = subprocess.run(
            ["loginctl", "show-user", str(os.getuid()), "-p", "Linger", "--value"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        value = result.stdout.strip().lower()
        return {
            "available": result.returncode == 0,
            "enabled": value == "yes" if result.returncode == 0 else None,
        }
