"""Read model options from each receiver's own CLI, without running an agent turn."""

import os
import subprocess
import tempfile
import time
from pathlib import Path
from threading import Lock

from agent_harness import executors
from agent_harness.appserver_client import AppServer
from agent_harness.common import REASONING_LEVELS, read_json, write_json
from agent_harness.dsh_mcp import home as dsh_home


_catalog_cache = {}
_cache_lock = Lock()
_known_levels = REASONING_LEVELS - {"default"}

_DSH_CATALOG = """
import { writeFileSync } from "node:fs";

export const name = "foolship-model-catalog";
export const inject = ["llm"];

export function apply(ctx, config) {
  // Allow the composed provider adapters to register before reading metadata.
  setTimeout(async () => {
    const provider = ctx.get("llm");
    const signal = AbortSignal.timeout(3000);
    const entries = await Promise.all(config.models.map(async model => {
      try {
        const metadata = await provider.resolveModelInfo(config.provider, model, signal);
        const reasoning = metadata.reasoning;
        return [model, {
          efforts: reasoning?.efforts.map(effort => effort.id) || [],
          default_effort: reasoning?.defaultEffort || null,
        }];
      } catch {
        return [model, null];
      }
    }));
    writeFileSync(config.result, JSON.stringify(Object.fromEntries(entries)), "utf8");
    process.exit(0);
  }, 50);
}
"""


def _capability(efforts, default):
    if not isinstance(efforts, list) or not all(isinstance(value, str) for value in efforts):
        return None
    levels = list(dict.fromkeys(level for level in efforts if level in _known_levels))
    return {"efforts": levels, "default_effort": default if default in levels else None}


def _binary(root, kind):
    deadline = time.monotonic() + 3

    def version(exe):
        remaining = deadline - time.monotonic()
        return executors.version(exe, timeout=remaining) if remaining > 0 else None

    return executors.binary(root, kind, version)


def _codex_catalog(root, exe, names):
    command = [exe, "app-server", "--stdio"]
    for feature in ("apps", "plugins", "remote_plugin", "skill_mcp_dependency_install"):
        command += ["--disable", feature]
    pending = {name.casefold() for name in names}
    found = {}
    with tempfile.TemporaryDirectory(prefix="foolship-codex-catalog-") as scratch:
        server = AppServer(
            command,
            scratch,
            executors.child_environment(root, kind=executors.CODEX),
            subprocess.DEVNULL,
            initialize_timeout=3,
        )
        try:
            deadline = time.monotonic() + 4
            params = {"limit": 100, "includeHidden": True}
            while pending and (remaining := deadline - time.monotonic()) > 0:
                try:
                    result = server.call("model/list", params, remaining)
                except TimeoutError:
                    break
                for model in result.get("data", []):
                    if not isinstance(model, dict):
                        continue
                    keys = {
                        value.casefold() for field in ("id", "model") if isinstance(value := model.get(field), str)
                    } & pending
                    if not keys:
                        continue
                    levels = model.get("supportedReasoningEfforts")
                    if not isinstance(levels, list) or not all(isinstance(entry, dict) for entry in levels):
                        continue
                    capability = _capability(
                        [entry.get("reasoningEffort") for entry in levels], model.get("defaultReasoningEffort")
                    )
                    found.update(dict.fromkeys(keys, capability))
                    pending.difference_update(keys)
                cursor = result.get("nextCursor")
                if not isinstance(cursor, str) or not cursor or cursor == params.get("cursor"):
                    break
                params["cursor"] = cursor
        finally:
            server.close(timeout=1)
    return found


def _dsh_catalog(root, exe, names):
    with tempfile.TemporaryDirectory(prefix="foolship-dsh-catalog-") as scratch:
        directory = Path(scratch)
        report, patch = directory / "catalog.json", directory / "patch.json"
        (directory / "catalog.js").write_text(_DSH_CATALOG, encoding="utf-8")
        write_json(
            patch,
            [
                {"id": "headless-runner", "disabled": True},
                {"id": "credentials", "disabled": False},
                {
                    "insert": [
                        {
                            "id": "foolship-model-catalog",
                            "name": "./catalog.js",
                            "config": {
                                "provider": executors.dsh_provider(root),
                                "models": names,
                                "result": str(report),
                            },
                        }
                    ]
                },
            ],
        )
        result = subprocess.run(
            [exe, "--profile", executors.DSH_PROFILE, "--patch", str(patch)],
            cwd=directory,
            env=executors.child_environment(root, kind=executors.DSH),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=executors._no_window(),
            timeout=7,
        )
        if result.returncode:
            return {}
        entries = read_json(report, {})
        return {
            name.casefold(): _capability(entry.get("efforts"), entry.get("default_effort"))
            for name, entry in entries.items()
            if name in names and isinstance(entry, dict)
        }


def _fingerprint(root, kind, names):
    files = [root / "executor.json"]
    if kind == executors.CODEX:
        home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        files.extend([home / "config.toml", home / "auth.json"])
    else:
        home = dsh_home()
        files.extend([home / "cordis.patch.yml", home / "profiles" / executors.DSH_PROFILE / "cordis.patch.yml"])
    stamps = []
    for file in files:
        try:
            stamps.append(file.stat().st_mtime_ns)
        except OSError:
            stamps.append(None)
    return tuple(names), tuple(stamps)


def _catalog(root, kind, names, refresh):
    key = (root.resolve(), kind)
    fingerprint = _fingerprint(root, kind, names)
    with _cache_lock:
        cached = _catalog_cache.get(key)
        if not refresh and cached and cached[0] == fingerprint and cached[1] > time.monotonic():
            return cached[2]
    try:
        exe = _binary(root, kind)
        loader = _codex_catalog if kind == executors.CODEX else _dsh_catalog
        result = loader(root, exe, names)
    except (OSError, RuntimeError, TypeError, ValueError, AttributeError, subprocess.TimeoutExpired):
        result = {}
    with _cache_lock:
        _catalog_cache[key] = (fingerprint, time.monotonic() + (180 if result else 30), result)
    return result


def snapshot(root, *, refresh=False):
    """Return capabilities for the receiver's configured models; None means unknown."""
    root = Path(root)
    models = executors.models(root)
    configured = executors.settings(root).get("model")
    override = (configured.strip() or None) if isinstance(configured, str) else None
    catalogs = {
        kind: _catalog(root, kind, names, refresh)
        for kind in executors.KINDS
        if (names := [entry["name"] for entry in models if entry["executor"] == kind])
    }
    return [
        {
            **entry,
            "efforts": None,
            "default_effort": None,
            **(catalogs[entry["executor"]].get(entry["name"].casefold()) or {}),
            "legacy_model_override": override if entry["executor"] == executors.DSH else None,
        }
        for entry in models
    ]


def for_model(root, model, *, refresh=False):
    """Get one configured model's capabilities from its executing receiver."""
    wanted = model.casefold()
    return next((entry for entry in snapshot(root, refresh=refresh) if entry["name"].casefold() == wanted), None)
