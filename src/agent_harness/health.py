"""Stable, privacy-bounded health reports shared by every runtime surface."""

from __future__ import annotations

import re
import time
from enum import StrEnum


HEALTH_SCHEMA_VERSION = 1
HEALTH_STATES = frozenset({"healthy", "degraded", "unavailable", "unknown"})
MAX_HEALTH_MESSAGE = 240


class HealthCode(StrEnum):
    OK = "OK"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    OFFLINE = "OFFLINE"
    NETWORK_UNREACHABLE = "NETWORK_UNREACHABLE"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    AUTH_REJECTED = "AUTH_REJECTED"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    CONFIG_INVALID = "CONFIG_INVALID"
    CLI_NOT_FOUND = "CLI_NOT_FOUND"
    CLI_NOT_LOGGED_IN = "CLI_NOT_LOGGED_IN"
    PROXY_UNREACHABLE = "PROXY_UNREACHABLE"
    STORAGE_ERROR = "STORAGE_ERROR"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    UNKNOWN = "UNKNOWN"


_SECRET = re.compile(
    r"(?i)\b(password|passwd|token|secret|authorization|private[_ -]?key|device[_ -]?key)\b\s*[:=]\s*[^\s,;]+"
)
_URL = re.compile(r"(?i)\b(?:https?|socks5?)://[^\s]+")
_WINDOWS_PATH = re.compile(r"(?i)\b[a-z]:[\\/][^\s,;]+")
_UNIX_PATH = re.compile(r"(?<![\w:])/(?:[^\s/,;]+/)+[^\s,;]*")


def safe_message(value, fallback="状态信息不可用"):
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    if not text:
        text = fallback
    text = _SECRET.sub(lambda match: match.group(1) + "=[redacted]", text)
    text = _URL.sub("[address]", text)
    text = _WINDOWS_PATH.sub("[path]", text)
    text = _UNIX_PATH.sub("[path]", text)
    return text[:MAX_HEALTH_MESSAGE]


def component(identifier, state, code, message="", retryable=False, updated_at=None):
    if not isinstance(identifier, str) or not 1 <= len(identifier) <= 100:
        raise ValueError("health component id is invalid")
    if state not in HEALTH_STATES:
        raise ValueError("health component state is invalid")
    try:
        code = HealthCode(code).value
    except (TypeError, ValueError):
        raise ValueError("health component code is invalid") from None
    return {
        "id": identifier,
        "state": state,
        "code": code,
        "message": safe_message(message, "运行正常" if state == "healthy" else "状态需要检查"),
        "retryable": bool(retryable),
        "updated_at": updated_at if isinstance(updated_at, (int, float)) else None,
    }


def report(components, generated_at=None):
    checked = [dict(item) for item in components]
    for item in checked:
        # Rebuild to reject accidental fields and normalize messages.
        normalized = component(
            item.get("id"),
            item.get("state"),
            item.get("code"),
            item.get("message", ""),
            item.get("retryable", False),
            item.get("updated_at"),
        )
        item.clear()
        item.update(normalized)
    states = {item["state"] for item in checked}
    if "unavailable" in states:
        overall = "unavailable"
    elif states.intersection({"degraded", "unknown"}):
        overall = "degraded"
    else:
        overall = "healthy"
    return {
        "schema_version": HEALTH_SCHEMA_VERSION,
        "generated_at": float(time.time() if generated_at is None else generated_at),
        "overall": overall,
        "components": checked,
    }


def normalize_exception(identifier, exc, *, code=None, message=None, retryable=None):
    if code is None:
        if isinstance(exc, FileNotFoundError):
            code, state, default_retry = HealthCode.NOT_CONFIGURED, "degraded", False
        elif isinstance(exc, PermissionError):
            code, state, default_retry = HealthCode.AUTH_REJECTED, "unavailable", False
        elif isinstance(exc, (TimeoutError, ConnectionError)):
            code, state, default_retry = HealthCode.NETWORK_UNREACHABLE, "unavailable", True
        elif isinstance(exc, ValueError):
            code, state, default_retry = HealthCode.CONFIG_INVALID, "degraded", False
        elif isinstance(exc, OSError):
            code, state, default_retry = HealthCode.SERVICE_UNAVAILABLE, "unavailable", True
        else:
            code, state, default_retry = HealthCode.UNKNOWN, "unknown", False
    else:
        code = HealthCode(code)
        state = "degraded" if code in {HealthCode.NOT_CONFIGURED, HealthCode.CONFIG_INVALID} else "unavailable"
        default_retry = code in {
            HealthCode.NETWORK_UNREACHABLE,
            HealthCode.SERVICE_UNAVAILABLE,
            HealthCode.OFFLINE,
            HealthCode.PROXY_UNREACHABLE,
        }
    if message is None:
        message = (
            "发生未分类错误；请在本机查看详细日志。"
            if code == HealthCode.UNKNOWN
            else safe_message(exc, "状态检查失败")
        )
    return component(identifier, state, code, message, default_retry if retryable is None else retryable)
