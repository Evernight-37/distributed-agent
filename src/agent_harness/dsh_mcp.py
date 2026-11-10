"""Install the Foolship collaboration MCP server into DeepSeek Harness.

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
MARKER = "# foolship: managed DeepSeek Harness collaboration MCP entry"
HOME_ENV = "DSH_HOME"
# The desktop app is the profile a person actually talks to DeepSeek Harness
# through. The one-shot headless profile used by dispatched turns is left alone,
# which is what makes this behave like an added feature rather than a global
# change.
DEFAULT_PROFILE = "desktop"


def home(environ=None):
    """Resolve the DeepSeek Harness home the same way its own launcher does."""
    value = (environ or os.environ).get(HOME_ENV, "")
    value = value.strip() if isinstance(value, str) else ""
    return Path(value).expanduser() if value else Path.home() / ".dsh"


def profile_dir(profile, root_home=None):
    return Path(root_home or home()) / "profiles" / profile


def patch_path(profile=DEFAULT_PROFILE, root_home=None):
    """The profile's own user patch layer.

    Writing here scopes the entry to one DeepSeek Harness profile, which is the
    closest thing to installing a plugin: the desktop app's sessions gain the
    tools while the one-shot ``headless`` profile this application drives stays
    untouched. DeepSeek Harness's own config editor writes the same file, and it
    replaces single entries instead of rewriting the document, so the two
    writers coexist.
    """
    return profile_dir(profile, root_home) / "cordis.patch.yml"


def server_name(mcp_name):
    """A stable, contract-legal MCP server name for one Foolship profile.

    DeepSeek Harness requires ``[A-Za-z0-9_-]{1,32}`` and rejects two entries
    with the same name, so a long profile-derived name is shortened with a hash
    instead of being truncated into a collision.
    """
    import hashlib

    digest = hashlib.sha256(mcp_name.encode("utf-8")).hexdigest()[:8]
    return "foolship-" + digest


def entry(launch, name):
    """The one loader entry that registers the Foolship MCP server."""
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
        return "DeepSeek Harness 的 home 补丁不是顶层块状列表，Foolship 不会改写它"
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
    temporary = path.with_name(path.name + ".foolship.tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def recorded_launch(root_home=None, profile=DEFAULT_PROFILE):
    """The command the managed entry currently names, if it can be read."""
    path = patch_path(profile, root_home)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    lines = text.splitlines()
    for index, raw in enumerate(lines):
        if raw.strip() != MARKER or index + 1 >= len(lines):
            continue
        candidate = lines[index + 1].strip()
        if not candidate.startswith("- "):
            continue
        try:
            document = json.loads(candidate[2:])
        except ValueError:
            continue
        command = document.get("command")
        if not isinstance(command, str):
            inserted = document.get("insert")
            if isinstance(inserted, list) and inserted and isinstance(inserted[0], dict):
                config = inserted[0].get("config")
                if isinstance(config, dict):
                    command = config.get("command")
        if isinstance(command, str) and command:
            return command
    return None


def repair(root_home=None, launch=None, name="foolship", probe=None, profile=DEFAULT_PROFILE):
    """Re-point a stale entry whose command no longer exists.

    Moving user data leaves the recorded launcher path pointing at the old
    location, and DeepSeek Harness then starts nothing. A command that still
    works is left alone: it may be a deliberate choice.
    """
    if not launch:
        return {"repaired": False, "reason": "缺少 MCP 启动命令"}
    state = status(root_home=root_home, launch=launch, name=name, profile=profile)
    if not state.get("installed") or not state.get("stale"):
        return {"repaired": False, **state}
    recorded = recorded_launch(root_home=root_home, profile=profile)
    if recorded and Path(recorded).exists() and Path(recorded) != Path(launch[0]):
        return {"repaired": False, "reason": "条目指向另一个仍然存在的启动器", **state}
    install(root_home=root_home, launch=launch, name=name, probe=probe, profile=profile)
    return {"repaired": True, **status(root_home=root_home, launch=launch, name=name, profile=profile)}


def status(root_home=None, launch=None, name="foolship", profile=DEFAULT_PROFILE):
    """Report whether this module's entry is present and still up to date."""
    path = patch_path(profile, root_home)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"installed": False, "stale": False, "path": str(path), "profile": profile}
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
        "profile": profile,
    }


def install(root_home=None, launch=None, name="foolship", probe=None, profile=DEFAULT_PROFILE):
    """Add or refresh this module's entry, then prove the file still composes."""
    if not launch:
        raise ValueError("缺少 MCP 启动命令")
    directory = profile_dir(profile, root_home)
    if not directory.is_dir():
        # A half-created profile would stop DeepSeek Harness from booting, so
        # only touch a profile the application itself already materialized.
        raise ValueError(
            "找不到 DeepSeek Harness profile " + profile + "；请先启动一次 DeepSeek Harness 桌面应用再连接"
        )
    path = patch_path(profile, root_home)
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
        backup = path.with_name(path.name + ".foolship.bak")
        backup.write_bytes(path.read_bytes())
    _write(path, text)
    result = {
        "installed": True,
        "path": str(path),
        "profile": profile,
        "backup": str(backup) if backup else None,
    }
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


def remove(root_home=None, profile=DEFAULT_PROFILE):
    """Remove this module's entry and leave every other line untouched."""
    path = patch_path(profile, root_home)
    try:
        existing = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"installed": False, "removed": False, "path": str(path), "profile": profile}
    if MARKER not in existing:
        return {"installed": False, "removed": False, "path": str(path), "profile": profile}
    text = _strip_managed(existing)
    if _remaining_is_empty(text):
        text = "".join(raw for raw in text.splitlines(keepends=True) if raw.strip() != "[]") + "[]\n"
    backup = path.with_name(path.name + ".foolship.bak")
    backup.write_bytes(path.read_bytes())
    _write(path, text)
    return {"installed": False, "removed": True, "path": str(path), "profile": profile, "backup": str(backup)}


def snippet(launch, name="foolship"):
    """The exact lines a user can paste when the file must not be edited."""
    return MARKER + "\n" + line(launch, name) + "\n"
