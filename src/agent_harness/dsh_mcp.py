"""Install the Harness collaboration MCP server into DeepSeek Harness.

Codex owns its own configuration API (``codex mcp add``).  DeepSeek Harness has
no equivalent command, but it composes every profile on top of one user patch
layer, ``$DSH_HOME/cordis.patch.yml``, which is read for every profile.  That
file is the closest equivalent and the only injection point that does not
require the user to change how they launch ``dsh``.

Because that file is the user's own configuration and a malformed entry stops
*every* DeepSeek Harness profile from booting, this module never rewrites it:

* it only appends or removes the two lines it owns, identified by a marker;
* it refuses to touch a file that is not demonstrably a top-level block
  sequence, and asks the user to paste the snippet instead;
* it keeps one rolling backup and can roll back when a caller supplies a probe.

The owned entry is a single JSON object, which is valid YAML flow syntax, so the
quoting of Windows paths and non-ASCII user names is left to ``json.dumps``
instead of a hand-written template.
"""

import json
import os
from pathlib import Path

SERVER_MODULE = "@deepseek-ai/dsh-mcp-client"
ENTRY_ID = "harness-collab"
MARKER = "# agent-harness: managed DeepSeek Harness collaboration MCP entry"
HOME_ENV = "DSH_HOME"


def home(environ=None):
    """Resolve the DeepSeek Harness home the same way its own launcher does."""
    value = (environ or os.environ).get(HOME_ENV, "")
    value = value.strip() if isinstance(value, str) else ""
    return Path(value).expanduser() if value else Path.home() / ".dsh"


def patch_path(root_home=None):
    return Path(root_home or home()) / "cordis.patch.yml"


def server_name(mcp_name):
    """A stable, contract-legal MCP server name for one Harness profile.

    DeepSeek Harness requires ``[A-Za-z0-9_-]{1,32}`` and rejects two entries
    with the same name, so a long profile-derived name is shortened with a hash
    instead of being truncated into a collision.
    """
    import hashlib

    digest = hashlib.sha256(mcp_name.encode("utf-8")).hexdigest()[:8]
    return "harness-" + digest


def entry(launch, name):
    """The one loader entry that registers the Harness MCP server."""
    if not launch or not all(isinstance(item, str) and item for item in launch):
        raise ValueError("MCP 启动命令无效")
    return {
        "insert": [
            {
                "id": ENTRY_ID,
                "name": SERVER_MODULE,
                "config": {
                    "serverName": name,
                    "transport": "stdio",
                    "command": launch[0],
                    "args": list(launch[1:]),
                },
            }
        ]
    }


def line(launch, name):
    # Single-line JSON is valid YAML flow syntax and needs no quoting rules of
    # our own.  ensure_ascii keeps non-ASCII paths inside ASCII escapes.
    return "- " + json.dumps(entry(launch, name), ensure_ascii=True, separators=(", ", ": "))


def _appendable(text):
    """Return None when new items can be appended safely, else the reason."""
    if not text.strip():
        return None
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or stripped in {"[]", "---"}:
            continue
        if raw[:2] in {"- ", "-\t"}:
            continue
        if raw[0] in {" ", "\t"}:
            continue
        return "DeepSeek Harness 的 home 补丁不是顶层块状列表，Harness 不会改写它"
    return None


def _strip_managed(text):
    """Remove the marker and the single entry line this module owns."""
    lines = text.splitlines(keepends=True)
    kept = []
    index = 0
    while index < len(lines):
        raw = lines[index]
        if raw.strip() == MARKER:
            index += 1
            if index < len(lines):
                following = lines[index]
                if following.startswith("- ") and ENTRY_ID in following:
                    index += 1
            continue
        kept.append(raw)
        index += 1
    return "".join(kept)


def _remaining_is_empty(text):
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or stripped in {"[]", "---"}:
            continue
        return False
    return True


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".agent-harness.tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def status(root_home=None, launch=None, name="harness"):
    """Report whether this module's entry is present and still up to date."""
    path = patch_path(root_home)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"installed": False, "stale": False, "path": str(path), "home": str(path.parent)}
    installed = MARKER in text
    current = None
    lines = text.splitlines()
    for index, raw in enumerate(lines):
        if raw.strip() == MARKER and index + 1 < len(lines):
            current = lines[index + 1].strip()
    expected = line(launch, name) if launch else None
    return {
        "installed": installed,
        "stale": bool(installed and expected and current != expected),
        "path": str(path),
        "home": str(path.parent),
    }


def install(root_home=None, launch=None, name="harness", probe=None):
    """Add or refresh this module's entry, then prove the file still composes."""
    if not launch:
        raise ValueError("缺少 MCP 启动命令")
    path = patch_path(root_home)
    try:
        existing = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        existing = ""
    reason = _appendable(existing)
    if reason:
        raise ValueError(reason)
    base = _strip_managed(existing)
    if "\n" in base and not base.endswith("\n"):
        base += "\n"
    if _remaining_is_empty(base):
        # Keep the user's comments, drop a bare empty document marker.
        base = "".join(raw for raw in base.splitlines(keepends=True) if raw.strip() != "[]")
    text = base + MARKER + "\n" + line(launch, name) + "\n"
    backup = None
    if path.exists():
        backup = path.with_name(path.name + ".agent-harness.bak")
        backup.write_bytes(path.read_bytes())
    _write(path, text)
    result = {"installed": True, "path": str(path), "home": str(path.parent), "backup": str(backup) if backup else None}
    if probe is not None:
        try:
            probe()
        except Exception as exc:
            if backup is not None:
                _write(path, backup.read_text(encoding="utf-8"))
            else:
                path.unlink(missing_ok=True)
            raise RuntimeError("写入后 DeepSeek Harness 无法加载配置，已回滚：" + str(exc)) from exc
    return result


def remove(root_home=None):
    """Remove this module's entry and leave every other line untouched."""
    path = patch_path(root_home)
    try:
        existing = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"installed": False, "removed": False, "path": str(path)}
    if MARKER not in existing:
        return {"installed": False, "removed": False, "path": str(path)}
    text = _strip_managed(existing)
    if _remaining_is_empty(text):
        text = "".join(raw for raw in text.splitlines(keepends=True) if raw.strip() != "[]") + "[]\n"
    backup = path.with_name(path.name + ".agent-harness.bak")
    backup.write_bytes(path.read_bytes())
    _write(path, text)
    return {"installed": False, "removed": True, "path": str(path), "backup": str(backup)}


def snippet(launch, name="harness"):
    """The exact lines a user can paste when the file must not be edited."""
    return MARKER + "\n" + line(launch, name) + "\n"
