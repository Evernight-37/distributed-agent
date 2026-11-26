"""Machine-local interface preferences and native display language detection."""

import os
import re
import sys
from pathlib import Path

from agent_harness.common import read_json, write_json


LANGUAGE_PREFERENCES = ("system", "zh-CN", "en")
_PREFERENCES_FILE = "interface-language.json"
_LOCALE_TAG = re.compile(r"[a-zA-Z]{2,3}(?:-[a-zA-Z0-9]{2,8})*")


def _locale_tag(value):
    if not isinstance(value, str):
        return None
    tag = re.split(r"[.@]", value.strip(), maxsplit=1)[0].replace("_", "-")
    return tag if _LOCALE_TAG.fullmatch(tag) else None


def _windows_language():
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    get_language = kernel.GetUserDefaultUILanguage
    get_language.argtypes = ()
    get_language.restype = wintypes.WORD
    locale_name = kernel.LCIDToLocaleName
    locale_name.argtypes = (wintypes.DWORD, wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD)
    locale_name.restype = ctypes.c_int
    buffer = ctypes.create_unicode_buffer(85)  # LOCALE_NAME_MAX_LENGTH
    if locale_name(get_language(), buffer, len(buffer), 0):
        return buffer.value
    return None


def _mac_language():
    from Foundation import NSLocale

    languages = NSLocale.preferredLanguages()
    return str(languages[0]) if languages else None


def system_language():
    """Read the display language, independently of the account and project."""
    if sys.platform in {"win32", "darwin"}:
        try:
            language = _windows_language() if sys.platform == "win32" else _mac_language()
        except (AttributeError, ImportError, OSError):
            language = None
        tag = _locale_tag(language)
        if tag:
            return tag
    candidates = (
        os.environ.get("LC_ALL"),
        *os.environ.get("LANGUAGE", "").split(":"),
        os.environ.get("LC_MESSAGES"),
        os.environ.get("LANG"),
    )
    for value in candidates:
        tag = _locale_tag(value)
        if tag:
            return tag
    return None


def settings(root):
    try:
        record = read_json(Path(root) / _PREFERENCES_FILE, {}, tolerant=True)
    except OSError:
        record = {}
    preference = record.get("preference") if isinstance(record, dict) else None
    return {
        "preference": preference if preference in LANGUAGE_PREFERENCES else "system",
        "system_language": system_language(),
    }


def save(root, preference):
    if preference not in LANGUAGE_PREFERENCES:
        raise ValueError("Invalid interface language preference")
    write_json(Path(root) / _PREFERENCES_FILE, {"preference": preference})
    return settings(root)


def resolved_language(value):
    preference = value["preference"]
    if preference != "system":
        return preference
    primary = (value["system_language"] or "").split("-", 1)[0].lower()
    return "zh-CN" if primary == "zh" else "en"


def window_title(language):
    return "Foolship · 协作工作台" if language == "zh-CN" else "Foolship · Workspace"
