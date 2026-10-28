"""Operating-system protection for local encrypted identity blobs."""

from __future__ import annotations

import base64
import os
import secrets
import shutil
import subprocess
import sys


_ENTROPY = b"AgentHarness local identity v2"
_SERVICE = "AgentHarness.Identity"
_TIMEOUT = 10


def protect(value):
    """Return an OS-bound envelope, or ``None`` for encrypted-file fallback."""

    if not isinstance(value, bytes):
        raise TypeError("secret value must be bytes")
    if os.name == "nt":
        return {"backend": "windows-dpapi", "payload": base64.b64encode(_cryptprotect(value)).decode("ascii")}
    if sys.platform == "darwin":
        return _protect_macos(value)
    if sys.platform.startswith("linux"):
        return _protect_linux(value)
    return None


def unprotect(envelope):
    if not isinstance(envelope, dict):
        raise ValueError("unsupported OS secret backend")
    backend = envelope.get("backend")
    if backend == "windows-dpapi":
        if os.name != "nt":
            raise PermissionError("this identity is protected for a Windows account")
        try:
            payload = base64.b64decode(envelope["payload"], validate=True)
        except Exception:
            raise PermissionError("unable to unlock OS-protected identity") from None
        return _cryptunprotect(payload)
    if backend == "macos-keychain":
        return _unprotect_macos(envelope)
    if backend == "linux-secret-service":
        return _unprotect_linux(envelope)
    raise ValueError("unsupported OS secret backend")


def _reference(envelope, backend, platform):
    if sys.platform != platform and not (platform == "linux" and sys.platform.startswith("linux")):
        raise PermissionError("this identity is protected for a different operating system")
    reference = envelope.get("reference")
    if envelope.get("backend") != backend or not isinstance(reference, str) or len(reference) != 32:
        raise PermissionError("unable to unlock OS-protected identity")
    try:
        int(reference, 16)
    except ValueError:
        raise PermissionError("unable to unlock OS-protected identity") from None
    return reference


def _protect_macos(value):
    reference = secrets.token_hex(16)
    try:
        _mac_keychain_add(reference, value)
    except OSError:
        return None
    return {"backend": "macos-keychain", "reference": reference}


def _unprotect_macos(envelope):
    reference = _reference(envelope, "macos-keychain", "darwin")
    try:
        return _mac_keychain_get(reference)
    except PermissionError:
        raise
    except Exception:
        raise PermissionError("unable to unlock OS-protected identity") from None


def _mac_security():
    import ctypes

    try:
        library = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Security.framework/Security")
    except OSError:
        raise OSError("macOS Keychain is unavailable") from None
    library.SecKeychainAddGenericPassword.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    library.SecKeychainAddGenericPassword.restype = ctypes.c_int32
    library.SecKeychainFindGenericPassword.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    library.SecKeychainFindGenericPassword.restype = ctypes.c_int32
    library.SecKeychainItemFreeContent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    library.SecKeychainItemFreeContent.restype = ctypes.c_int32
    return ctypes, library


def _mac_keychain_add(reference, value):
    ctypes, library = _mac_security()
    service = _SERVICE.encode("utf-8")
    account = reference.encode("ascii")
    service_buffer = ctypes.create_string_buffer(service)
    account_buffer = ctypes.create_string_buffer(account)
    value_buffer = ctypes.create_string_buffer(value)
    status = library.SecKeychainAddGenericPassword(
        None,
        len(service),
        ctypes.cast(service_buffer, ctypes.c_void_p),
        len(account),
        ctypes.cast(account_buffer, ctypes.c_void_p),
        len(value),
        ctypes.cast(value_buffer, ctypes.c_void_p),
        None,
    )
    if status:
        raise OSError("macOS Keychain could not protect the local identity")


def _mac_keychain_get(reference):
    ctypes, library = _mac_security()
    service = _SERVICE.encode("utf-8")
    account = reference.encode("ascii")
    service_buffer = ctypes.create_string_buffer(service)
    account_buffer = ctypes.create_string_buffer(account)
    length = ctypes.c_uint32()
    data = ctypes.c_void_p()
    status = library.SecKeychainFindGenericPassword(
        None,
        len(service),
        ctypes.cast(service_buffer, ctypes.c_void_p),
        len(account),
        ctypes.cast(account_buffer, ctypes.c_void_p),
        ctypes.byref(length),
        ctypes.byref(data),
        None,
    )
    if status:
        raise PermissionError("unable to unlock OS-protected identity")
    try:
        return ctypes.string_at(data, length.value)
    finally:
        library.SecKeychainItemFreeContent(None, data)


def _secret_tool():
    return shutil.which("secret-tool")


def _protect_linux(value):
    command = _secret_tool()
    if not command:
        return None
    reference = secrets.token_hex(16)
    encoded = base64.b64encode(value).decode("ascii")
    try:
        result = subprocess.run(
            [
                command,
                "store",
                "--label=Agent Harness identity",
                "service",
                "agent-harness",
                "purpose",
                "local-identity",
                "reference",
                reference,
            ],
            input=encoded,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode:
        return None
    return {"backend": "linux-secret-service", "reference": reference}


def _unprotect_linux(envelope):
    reference = _reference(envelope, "linux-secret-service", "linux")
    command = _secret_tool()
    if not command:
        raise PermissionError("Linux Secret Service is unavailable")
    try:
        result = subprocess.run(
            [
                command,
                "lookup",
                "service",
                "agent-harness",
                "purpose",
                "local-identity",
                "reference",
                reference,
            ],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
        )
        if result.returncode or not result.stdout.strip():
            raise PermissionError("unable to unlock OS-protected identity")
        return base64.b64decode(result.stdout.strip(), validate=True)
    except PermissionError:
        raise
    except Exception:
        raise PermissionError("unable to unlock OS-protected identity") from None


def _blob(value, ctypes, DataBlob):
    buffer = ctypes.create_string_buffer(value)
    return DataBlob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer


def _cryptprotect(value):
    import ctypes
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    source, source_buffer = _blob(value, ctypes, DataBlob)
    entropy, entropy_buffer = _blob(_ENTROPY, ctypes, DataBlob)
    output = DataBlob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    crypt.CryptProtectData.argtypes = [
        ctypes.POINTER(DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(DataBlob),
    ]
    crypt.CryptProtectData.restype = wintypes.BOOL
    if not crypt.CryptProtectData(
        ctypes.byref(source),
        "Agent Harness",
        ctypes.byref(entropy),
        None,
        None,
        1,
        ctypes.byref(output),
    ):
        raise OSError(ctypes.get_last_error(), "Windows could not protect the local identity")
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel.LocalFree(output.pbData)
        del source_buffer, entropy_buffer


def _cryptunprotect(value):
    import ctypes
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    source, source_buffer = _blob(value, ctypes, DataBlob)
    entropy, entropy_buffer = _blob(_ENTROPY, ctypes, DataBlob)
    output = DataBlob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    crypt.CryptUnprotectData.argtypes = [
        ctypes.POINTER(DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(DataBlob),
    ]
    crypt.CryptUnprotectData.restype = wintypes.BOOL
    description = wintypes.LPWSTR()
    if not crypt.CryptUnprotectData(
        ctypes.byref(source),
        ctypes.byref(description),
        ctypes.byref(entropy),
        None,
        None,
        1,
        ctypes.byref(output),
    ):
        raise PermissionError("unable to unlock OS-protected identity")
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        if description:
            kernel.LocalFree(description)
        kernel.LocalFree(output.pbData)
        del source_buffer, entropy_buffer
