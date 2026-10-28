"""Allowlist-only support snapshots safe to share with another person."""

from __future__ import annotations

import json
import platform
import sys
import time

from agent_harness.health import HEALTH_SCHEMA_VERSION, HealthCode, report


DIAGNOSTICS_SCHEMA_VERSION = 1
MAX_DIAGNOSTICS_BYTES = 64 * 1024


def build_support_snapshot(health, *, app_version="unknown", capabilities=None, counts=None):
    if not isinstance(health, dict) or health.get("schema_version") != HEALTH_SCHEMA_VERSION:
        raise ValueError("health report is invalid")
    # Rebuild through the health schema and deliberately omit every message,
    # identity, URL, path, configuration value and raw log field.
    normalized = report(health.get("components", []), health.get("generated_at", time.time()))
    public_prefixes = {"codex", "hub", "network", "proxy", "receiver", "receiver-service", "storage", "tailscale"}
    components = [
        {
            "id": item["id"] if item["id"].split(":", 1)[0] in public_prefixes else "component:" + str(index),
            "state": item["state"],
            "code": item["code"],
            "retryable": item["retryable"],
        }
        for index, item in enumerate(normalized["components"], 1)
    ]
    allowed_capabilities = {
        str(key): bool(value)
        for key, value in (capabilities or {}).items()
        if isinstance(key, str) and 1 <= len(key) <= 64 and isinstance(value, bool)
    }
    allowed_counts = {
        str(key): min(value, 1_000_000)
        for key, value in (counts or {}).items()
        if isinstance(key, str)
        and 1 <= len(key) <= 64
        and isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
    }
    snapshot = {
        "schema_version": DIAGNOSTICS_SCHEMA_VERSION,
        "generated_at": time.time(),
        "app_version": str(app_version)[:40],
        "platform": platform.system(),
        "architecture": platform.machine(),
        "python": ".".join(map(str, sys.version_info[:3])),
        "health": {
            "schema_version": normalized["schema_version"],
            "overall": normalized["overall"],
            "components": components,
        },
        "capabilities": allowed_capabilities,
        "counts": allowed_counts,
    }
    encoded = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_DIAGNOSTICS_BYTES:
        raise ValueError("diagnostics snapshot is too large")
    return snapshot


def validate_public_snapshot(snapshot):
    """Defense-in-depth validator for a snapshot before export."""

    forbidden = {
        "account",
        "agent",
        "attachment",
        "authorization",
        "body",
        "command",
        "database",
        "device",
        "identity",
        "key",
        "log",
        "message",
        "password",
        "path",
        "project",
        "proxy",
        "report",
        "server",
        "task",
        "token",
        "url",
    }

    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                words = set(str(key).lower().replace("-", "_").split("_"))
                if words.intersection(forbidden):
                    raise ValueError("diagnostics contains a forbidden field")
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(snapshot)
    # Ensure code values remain a closed public enum.
    for item in snapshot.get("health", {}).get("components", []):
        HealthCode(item.get("code"))
    return True
