"""Local agent CLI executors: Codex and DeepSeek Harness.

A machine runs exactly one local executor, selected in its receiver
``executor.json``.  The executor owns the argument vector, the permission
environment and the provider event vocabulary.  Everything above this module
(the worker loop, the broker, the coordinator and the desktop UI) only sees the
normalized events below, so adding an executor never means teaching the rest of
the application a second dialect.

Normalized events
-----------------
``thread.started``
    ``{"type": "thread.started", "thread_id": str}`` — one per turn, emitted
    before any work happens, and the value the receiver stores for resumption.
``item.completed``
    ``{"type": "item.completed", "item": {"type": "agent_message", "text": str}}``
    — the final human-facing answer of the turn.
``turn.completed``
    ``{"type": "turn.completed"}`` — the provider reports a completed turn.
``turn.failed``
    ``{"type": "turn.failed", "error": str}`` — the provider reports a failed turn.

Codex emits this vocabulary natively.  DeepSeek Harness emits its own
``session``/``status``/``text``/``tool_call``/``tool_result``/``turn_end``/
``final``/``error`` stream and is projected onto it by :func:`normalize`.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

from agent_harness.common import read_json, write_json

CODEX = "codex"
DSH = "dsh"
KINDS = (CODEX, DSH)

# The one-shot DSH profile this application drives.  ``dsh`` materializes it
# from the shipped template on first use, so no user setup is required.
DSH_PROFILE = "headless"
DSH_PROVIDER = "deepseek-official"
DSH_MODEL = "deepseek-flash"

# The managed-turn structured reply is enforced by a Codex JSON schema.  DSH has
# no equivalent flag, so its managed turns rely on the prompt contract alone.
MANAGED_TURNS = {CODEX: True, DSH: False}


def _no_window():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _label(kind):
    return "Codex" if kind == CODEX else "DeepSeek Harness"


def settings(root):
    """Return the validated executor settings of one receiver root."""
    value = read_json(Path(root) / "executor.json", {})
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("本地执行器设置无效")
    kind = value.get("executor", CODEX)
    if kind not in KINDS:
        raise ValueError("未知的本地执行器")
    return value


def selected(root):
    """The executor this machine runs."""
    return settings(root).get("executor", CODEX)


def supports_managed_turns(root):
    """Whether group-chat turns can run on the low-latency app-server path."""
    return MANAGED_TURNS.get(selected(root), False)


def version(exe):
    """Probe a candidate binary without making a model request."""
    try:
        result = subprocess.run(
            [str(exe), "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            creationflags=_no_window(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", result.stdout or "")
    return tuple(map(int, match.groups())) if match else None


def _codex_candidates():
    import shutil

    paths = []
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            paths.extend((Path(local) / "OpenAI" / "Codex" / "bin").glob("*/codex.exe"))
        roaming = os.environ.get("APPDATA")
        if roaming:
            paths.extend(Path(roaming) / "npm" / name for name in ("codex.cmd", "codex.exe"))
    elif sys.platform == "darwin":
        # Finder-launched apps do not inherit the interactive shell's PATH.
        paths.extend(
            Path(name).expanduser()
            for name in (
                "/Applications/Codex.app/Contents/Resources/codex",
                "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex",
                "~/Applications/Codex.app/Contents/Resources/codex",
                "/opt/homebrew/bin/codex",
                "/usr/local/bin/codex",
                "~/.npm-global/bin/codex",
                "~/.local/bin/codex",
            )
        )
        paths.extend((Path.home() / ".nvm" / "versions" / "node").glob("*/bin/codex"))
    for name in ("codex", "codex.exe", "codex.cmd"):
        found = shutil.which(name)
        if found:
            paths.append(Path(found))
    return paths


def _dsh_candidates():
    """Installed DeepSeek Harness launchers plus whatever PATH already exposes."""
    import shutil

    paths = []
    if os.name == "nt":
        # The desktop installer is per-user by default; a machine-wide install
        # lands under Program Files instead.
        for base in (
            os.environ.get("LOCALAPPDATA"),
            os.environ.get("ProgramFiles"),
            os.environ.get("ProgramFiles(x86)"),
        ):
            if not base:
                continue
            programs = Path(base) / "Programs" if base == os.environ.get("LOCALAPPDATA") else Path(base)
            paths.append(programs / "DeepSeek Harness" / "resources" / "runtime" / "cli" / "bin" / "dsh.cmd")
    elif sys.platform == "darwin":
        paths.extend(
            Path(name).expanduser()
            for name in (
                "/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh",
                "~/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh",
                "/opt/homebrew/bin/dsh",
                "/usr/local/bin/dsh",
                "~/.local/bin/dsh",
            )
        )
    else:
        paths.append(Path.home() / ".local" / "bin" / "dsh")
    for name in ("dsh", "dsh.cmd", "dsh.exe", "dsh.bat"):
        found = shutil.which(name)
        if found:
            paths.append(Path(found))
    return paths


def status(root):
    """Find a working local executor binary, honouring an explicit selection."""
    return status_of(root, selected(root))


def status_of(root, kind):
    """Find a working binary of ``kind`` regardless of this machine's selection.

    The saved explicit path belongs to the selected executor only; a probe for
    the other one has to discover its own binary.
    """
    root = Path(root)
    label = _label(kind)
    explicit = settings(root).get("cli_path") if kind == selected(root) else None
    if explicit:
        if not isinstance(explicit, str) or not Path(explicit).is_absolute() or not Path(explicit).is_file():
            return {
                "found": False,
                "configured": True,
                "error": "设置的 %s 路径已失效，请重新选择程序" % label,
            }
        found_version = version(explicit)
        if found_version is None:
            return {
                "found": False,
                "configured": True,
                "error": "所选程序无法运行 %s CLI，请重新选择" % label,
            }
        return {
            "found": True,
            "configured": True,
            "path": explicit,
            "version": ".".join(map(str, found_version)),
        }
    candidates = _codex_candidates() if kind == CODEX else _dsh_candidates()
    seen, valid = set(), []
    for candidate in candidates:
        key = str(candidate).casefold() if os.name == "nt" else str(candidate)
        if key in seen or not candidate.is_file():
            continue
        seen.add(key)
        found_version = version(candidate)
        if found_version is not None:
            valid.append((found_version, str(candidate)))
    if not valid:
        if kind == CODEX:
            message = "未找到可运行的 Codex CLI；请在本机安装 Codex，或在设置中选择程序"
        else:
            message = "未找到可运行的 DeepSeek Harness CLI；请安装 DeepSeek Harness，或在设置中选择程序"
        return {"found": False, "configured": False, "error": message}
    best_version, path = max(valid)
    return {
        "found": True,
        "configured": False,
        "path": path,
        "version": ".".join(map(str, best_version)),
    }


def login(root, exe):
    """Report whether the selected executor can authenticate, without reading secrets."""
    if selected(root) == DSH:
        # DSH headless resolves credentials from DEEPSEEK_API_KEY or from the
        # credential service the desktop Models page writes to.  Presence of the
        # environment variable is the only check that costs nothing and reads no
        # secret; a stored key is detected by the run itself.
        if os.environ.get("DEEPSEEK_API_KEY", "").strip():
            return {"logged_in": True}
        return {
            "logged_in": True,
            "credential_warning": (
                "未检测到环境变量 DEEPSEEK_API_KEY。若任务报 MISSING_CREDENTIAL，"
                "请在 DeepSeek Harness 的 Models 页面保存密钥，或为本机设置该环境变量"
            ),
        }
    try:
        root = Path(root)
        result = subprocess.run(
            [str(exe), "login", "status"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=child_environment(root),
            timeout=12,
            creationflags=_no_window(),
        )
    except subprocess.TimeoutExpired:
        return {
            "logged_in": False,
            "login_error": "Codex 登录状态检查超时；请检查本机代理",
        }
    except OSError:
        return {"logged_in": False, "login_error": "无法检查 Codex 登录状态"}
    output = (result.stdout + "\n" + result.stderr).casefold()
    if result.returncode == 0 and "not logged in" not in output:
        return {"logged_in": True}
    return {
        "logged_in": False,
        "login_error": "Codex CLI 尚未登录；请使用当前系统用户完成登录",
    }


def child_environment(root, stage=None, full_access=False):
    """The exact environment one executor child process inherits.

    The receiver's saved proxy and network environment apply to every executor,
    and DSH additionally learns its sandbox mode from the environment.
    """
    from agent_harness.common import apply_local_proxy

    root = Path(root)
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    setting = read_json(root / "executor.json", {})
    if setting is None:
        setting = {}
    network = setting.get("network_env", {})
    if not isinstance(network, dict) or set(network) - {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "WS_PROXY",
        "WSS_PROXY",
    }:
        raise ValueError("Unsupported worker network environment")
    for key, value in network.items():
        if not isinstance(value, str) or "\n" in value or "\r" in value:
            raise ValueError("Invalid network environment")
        env[key] = value
    if "proxy" in setting:
        env = apply_local_proxy(env, setting["proxy"])
    env.update(environment(root, stage, full_access))
    return env


def environment(root, stage=None, full_access=False):
    """Extra child-process environment for one turn of the selected executor.

    DSH reads its sandbox mode from ``DSH_PERMISSION_MODE``; Codex receives the
    equivalent as command-line flags instead.
    """
    if selected(root) != DSH:
        return {}
    if full_access:
        mode = "danger-full-access"
    else:
        mode = "workspace-write" if stage == "implement" else "read-only"
    return {"DSH_PERMISSION_MODE": mode}


def requested_model(root, project_model):
    """The model this machine actually asks for when a turn is dispatched.

    An explicit ``model`` in the receiver executor settings wins, because one
    machine runs one executor and therefore one provider route.  Otherwise the
    model configured for the agent is passed through unchanged.
    """
    override = settings(root).get("model")
    if isinstance(override, str) and override.strip():
        return override.strip()
    return project_model


def _dsh_patch(root, model):
    """The loader patch that pins DSH to this turn's model and permissions.

    Every entry replaces its config wholesale, so the permission presets are
    restated in full: a preset whose sandbox/approval pair matches no composed
    default makes DSH refuse to start.
    """
    return [
        {
            "id": "agent-default-model",
            "config": {"provider": settings(root).get("provider") or DSH_PROVIDER, "model": model},
        },
        # Unattended turns have no answerer; fail closed instead of hanging.
        {"id": "approval", "config": {"policy": "never"}},
        {
            "id": "permission",
            "config": {
                "presets": {
                    name: {"sandbox": name, "approval": "never"}
                    for name in ("read-only", "workspace-write", "danger-full-access")
                }
            },
        },
        # Let a key stored by the DeepSeek Harness Models page reach headless runs.
        {"id": "credentials", "disabled": False},
    ]


def _dsh_session_id(thread_id):
    if not isinstance(thread_id, str) or not re.fullmatch(r"session-[0-9a-fA-F-]{36}", thread_id):
        raise ValueError("Invalid session ID")
    return thread_id


def codex_command(exe, project, thread_id, managed=False, schema_path=None):
    """Codex ``exec`` invocation; its output already matches the contract."""
    args = [
        exe,
        "exec",
    ]
    if managed:
        args += [
            "--disable",
            "apps",
            "--disable",
            "plugins",
            "--disable",
            "remote_plugin",
            "--disable",
            "skill_mcp_dependency_install",
        ]
    args += [
        "--ignore-user-config",
        "--skip-git-repo-check",
        "--json",
        "-m",
        project["model"],
        "-c",
        'model_reasoning_effort="' + project["effort"] + '"',
        "-C",
        project["remote_path"],
    ]
    if project.get("local_control"):
        stage = project.get("_stage", "discuss")
        args += [
            "--sandbox",
            "workspace-write" if stage == "implement" else "read-only",
            "-c",
            'approval_policy="never"',
        ]
    else:
        args += ["--approve-for-me"]
    if thread_id:
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", thread_id):
            raise ValueError("Invalid session ID")
        args += ["resume", thread_id]
    if managed and schema_path:
        args += ["--output-schema", str(schema_path)]
    return args + ["-"]


def dsh_command(root, exe, project, thread_id, scratch):
    """DSH one-shot invocation that reads the task from stdin."""
    model = requested_model(root, project["model"])
    scratch = Path(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    patch_path = scratch / "dsh-patch.json"
    write_json(patch_path, _dsh_patch(root, model))
    args = [exe, "--profile", DSH_PROFILE, "--patch", str(patch_path), "--json"]
    if thread_id:
        args += ["--session-id", _dsh_session_id(thread_id)]
    # ``-`` is the task, read from stdin like the prompt file the worker opens.
    return args + ["-"]


def command(root, exe, project, thread_id, scratch=None, managed=False, schema_path=None):
    """Build one turn's argv for the executor this machine runs."""
    if selected(root) == DSH:
        return dsh_command(root, exe, project, thread_id, scratch)
    return codex_command(exe, project, thread_id, managed, schema_path)


def normalize(kind, event):
    """Project one provider event onto zero or more normalized events."""
    if kind == CODEX:
        # Codex already speaks the normalized vocabulary.
        return [event]
    if not isinstance(event, dict):
        return []
    kind_name = event.get("type")
    if kind_name == "session":
        session_id = event.get("sessionId")
        if isinstance(session_id, str) and session_id:
            return [{"type": "thread.started", "thread_id": session_id}]
        return []
    if kind_name == "status" and event.get("phase") == "turn_end":
        reason = event.get("reason") or {}
        if reason.get("kind") == "completed":
            return [{"type": "turn.completed"}]
        error = (reason.get("error") or {}).get("message") or reason.get("kind") or "turn failed"
        return [{"type": "turn.failed", "error": str(error)}]
    if kind_name == "final":
        text = event.get("text")
        return [
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": text if isinstance(text, str) else ""},
            }
        ]
    if kind_name == "error":
        return [{"type": "turn.failed", "error": str(event.get("message") or "runner error")}]
    # Thinking, streamed text, tool traffic and step bookkeeping are diagnostics;
    # the finalized answer arrives in ``final`` and the durable outcome in ``turn_end``.
    return []


def effective_model(kind, thread_id, since=None):
    """Read back the model the provider actually used, when it records one."""
    if kind != CODEX or not thread_id:
        # DSH records no per-turn model in a form this application can verify.
        return None
    from datetime import datetime

    from agent_harness.common import events, tail

    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    for session in (home / "sessions").rglob("*" + thread_id + "*.jsonl"):
        model = None
        for event in events(tail(session, 2 * 1024 * 1024)):
            if event["type"] == "turn_context":
                if since:
                    timestamp = event.get("timestamp")
                    if not timestamp or datetime.fromisoformat(
                        timestamp.replace("Z", "+00:00")
                    ) < datetime.fromisoformat(since):
                        continue
                model = event.get("payload", {}).get("model") or model
        if model:
            return model
    return None


def explain_failure(text):
    """Translate a provider-specific failure into an actionable Chinese message."""
    lowered = (text or "").lower()
    if "missing_credential" in lowered or "no api key" in lowered:
        return (
            "DeepSeek Harness 没有可用的 API Key：请在 DeepSeek Harness 的 Models 页面保存密钥，"
            "或让本机环境变量 DEEPSEEK_API_KEY 可用后重试"
        )
    if "composed sandbox and approval defaults match no preset" in lowered:
        return "DeepSeek Harness 的权限预设与应用配置冲突；请确认本机 dsh 版本未被修改后重试"
    if "does not exist; omit --session-id" in lowered or "cannot be adopted" in lowered:
        return "DeepSeek Harness 无法续接上一轮会话：会话已删除、工作目录已改变或被占用，请清除本项目的会话后重试"
    if "no such model" in lowered or "model not found" in lowered or "unknown model" in lowered:
        return "DeepSeek Harness 不认识本机为该项目配置的模型；请在本机执行器设置中指定 DSH 模型（例如 deepseek-flash）"
    return None
