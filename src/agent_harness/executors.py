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

# A machine runs whichever CLIs its owner installed, and the model decides which
# one executes a turn. Neither CLI can enumerate its models offline (Codex's come
# from the account, DeepSeek Harness's from its provider), so the table starts
# from what is actually configured and everything else is added by the operator.
DSH_DEFAULT_MODEL = DSH_MODEL


def codex_config_model(home=None):
    """The model Codex itself is configured with, if its config can be read.

    ``CODEX_HOME`` relocates the directory, exactly as it does for Codex.
    """
    base = home or os.environ.get("CODEX_HOME") or (Path.home() / ".codex")
    path = Path(base) / "config.toml"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    model = None
    try:
        import tomllib

        document = tomllib.loads(text)
        model = document.get("model")
    except Exception:
        # A config we cannot parse is not worth failing a settings page over.
        model = None
    if not isinstance(model, str) or not model.strip():
        for line in text.splitlines():
            match = re.match(r'^\s*model\s*=\s*"([^"]+)"\s*$', line)
            if match:
                model = match.group(1)
                break
    if isinstance(model, str) and model.strip():
        return model.strip()
    return None


def default_models(home=None):
    """The seed table: the model each installed CLI is actually set up with."""
    models = []
    codex = codex_config_model(home)
    if codex:
        models.append({"name": codex, "executor": CODEX})
    models.append({"name": DSH_DEFAULT_MODEL, "executor": DSH})
    return models


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


def models(root):
    """This machine's model table: which models it can run, and with which CLI.

    An empty or malformed table falls back to the defaults rather than leaving the
    machine unable to dispatch anything.
    """
    value = settings(root).get("models")
    if isinstance(value, list):
        cleaned, seen = [], set()
        for item in value:
            if not isinstance(item, dict):
                continue
            name, kind = item.get("name"), item.get("executor")
            if not isinstance(name, str) or not name.strip() or kind not in KINDS:
                continue
            name = name.strip()
            if name.casefold() in seen:
                continue
            seen.add(name.casefold())
            cleaned.append({"name": name, "executor": kind})
        if cleaned:
            return cleaned
    return default_models()


def model_kind(root, model):
    """Which CLI runs this model, or None when the machine's table lacks it."""
    if not isinstance(model, str):
        return None
    wanted = model.strip().casefold()
    for entry in models(root):
        if entry["name"].casefold() == wanted:
            return entry["executor"]
    return None


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


def status(root, probe=None):
    """Find a working local executor binary, honouring an explicit selection."""
    return status_of(root, selected(root), probe)


def cli_paths(root):
    """Explicit binaries per executor, as chosen in the desktop settings."""
    value = settings(root).get("cli_paths")
    if not isinstance(value, dict):
        return {}
    return {kind: path for kind, path in value.items() if kind in KINDS and isinstance(path, str) and path}


def status_of(root, kind, probe=None):
    """Find a working binary of ``kind`` regardless of this machine's selection.

    Each executor keeps its own explicit path, so a machine that runs both CLIs
    can point one of them at a hand-picked binary without affecting the other.
    ``probe`` lets the caller keep its own version hook (the worker exposes one
    and tests override it).
    """
    probe = probe or version
    root = Path(root)
    label = _label(kind)
    explicit = cli_paths(root).get(kind)
    if explicit:
        if not isinstance(explicit, str) or not Path(explicit).is_absolute() or not Path(explicit).is_file():
            return {
                "found": False,
                "configured": True,
                "error": "设置的 %s 路径已失效，请重新选择程序" % label,
            }
        found_version = probe(explicit)
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
        found_version = probe(candidate)
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


def login(root, exe, kind=None):
    """Report whether the executor that will run this turn can authenticate."""
    if (kind or selected(root)) == DSH:
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


def resolve_network(root, network=None):
    """Which proxy, if any, this turn may use.

    Two facts, deliberately kept apart: the machine owns the endpoint (a loopback
    address only means something there), the agent owns the policy. A policy of
    "machine" defers to the machine's saved endpoint, "direct" bypasses it even
    when one exists, and "custom" carries its own port.
    """
    if network is None:
        policy = {"mode": "machine"}
    else:
        if not isinstance(network, dict) or set(network) - {"mode", "port"}:
            raise ValueError("Invalid agent network setting")
        policy = network
    mode = policy.get("mode", "machine")
    if mode == "direct":
        return None
    if mode == "custom":
        port = policy.get("port")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("Proxy port must be between 1 and 65535")
        return {"enabled": True, "port": port}
    if mode != "machine":
        raise ValueError("Unknown agent network mode")
    setting = read_json(Path(root) / "executor.json", {})
    if not isinstance(setting, dict):
        return None
    proxy = setting.get("proxy")
    if not isinstance(proxy, dict):
        return None
    if not proxy.get("enabled"):
        return None
    return proxy


def child_environment(root, stage=None, full_access=False, network=None):
    """The exact environment one executor child process inherits.

    The proxy is resolved per turn from the agent's policy and the machine's
    endpoint, so a machine that has a proxy does not force every agent through it.
    DSH additionally learns its sandbox mode from the environment.
    """
    from agent_harness.common import apply_local_proxy

    root = Path(root)
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    setting = read_json(root / "executor.json", {})
    if setting is None:
        setting = {}
    network_env = setting.get("network_env", {})
    if not isinstance(network_env, dict) or set(network_env) - {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "WS_PROXY",
        "WSS_PROXY",
    }:
        raise ValueError("Unsupported worker network environment")
    for key, value in network_env.items():
        if not isinstance(value, str) or "\n" in value or "\r" in value:
            raise ValueError("Invalid network environment")
        env[key] = value
    proxy = resolve_network(root, network)
    if proxy is not None:
        env = apply_local_proxy(env, proxy)
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


def requested_model(root, project_model, kind=None):
    """The model this machine actually asks for when a turn is dispatched.

    Only DeepSeek Harness takes a receiver-level override, because its provider
    routes are configured per machine.  The override is ignored for Codex: a
    leftover value from a previous DeepSeek Harness selection must not make the
    effective-model check fail every turn.
    """
    if (kind or selected(root)) != DSH:
        return project_model
    override = settings(root).get("model")
    if isinstance(override, str) and override.strip():
        return override.strip()
    return project_model


def binary(root, kind=None, probe=None):
    """The executable that runs this kind, or a clear failure."""
    kind = kind or selected(root)
    found = status_of(root, kind, probe)
    if not found.get("found"):
        raise RuntimeError(found.get("error") or ("这台机器没有可用的 %s CLI" % _label(kind)))
    return found["path"]


def kind_for(root, model):
    """Which CLI has to run this model on this machine.

    The table is the closed set: a model it does not list is refused, because
    guessing a CLI would run the wrong one. Whether that CLI is actually installed
    is the next question (``binary``), so the two failures stay distinguishable.
    """
    kind = model_kind(root, model)
    if kind is None:
        raise ValueError("本机模型表里没有 " + str(model) + "；请先在本机模型表里添加该模型")
    return kind


# DeepSeek Harness validates the reasoning effort against the model's own
# catalog, and providers advertise a smaller vocabulary than the six Codex
# levels (deepseek-flash accepts off/low/high). ``medium`` is the Foolship
# default and is deliberately omitted so the model keeps its own default
# instead of failing on a level the provider may not advertise.
EFFORT_MAP = {"low": "low", "medium": None, "high": "high", "xhigh": "high", "max": "high", "ultra": "high"}


def reasoning_effort(effort):
    """Project one Foolship effort level onto DeepSeek Harness, or None to omit."""
    return EFFORT_MAP.get(effort if isinstance(effort, str) else "", None)


def _dsh_patch(root, model, effort=None):
    """The loader patch that pins DSH to this turn's model and permissions.

    Every entry replaces its config wholesale, so the permission presets are
    restated in full: a preset whose sandbox/approval pair matches no composed
    default makes DSH refuse to start.

    The collaboration MCP entry needs no handling here: it lives in the desktop
    profile's layer, and this one-shot turn runs the ``headless`` profile.
    """
    selection = {"provider": settings(root).get("provider") or DSH_PROVIDER, "model": model}
    level = reasoning_effort(effort)
    if level:
        selection["reasoningEffort"] = level
    patch = []
    patch.extend(
        [
            {"id": "agent-default-model", "config": selection},
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
    )
    return patch


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
    write_json(patch_path, _dsh_patch(root, model, project.get("effort")))
    args = [exe, "--profile", DSH_PROFILE, "--patch", str(patch_path), "--json"]
    if thread_id:
        args += ["--session-id", _dsh_session_id(thread_id)]
    # ``-`` is the task, read from stdin like the prompt file the worker opens.
    return args + ["-"]


def command(root, exe, project, thread_id, scratch=None, managed=False, schema_path=None, kind=None):
    """Build one turn's argv for the executor that model needs."""
    if (kind or selected(root)) == DSH:
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
    if "unsupported_reasoning_effort" in lowered or "does not support reasoning effort" in lowered:
        return (
            "所选模型不支持这个思考强度；请在员工设置里换一个档位，或改用该模型支持的模型。"
            "Foolship 会把 medium 视为“用模型默认档”，xhigh/max/ultra 按 high 发送"
        )
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
        return "DeepSeek Harness 不认识本机为该项目配置的模型；请在本机可用模型里指定 DSH 模型（例如 deepseek-flash）"
    return None
