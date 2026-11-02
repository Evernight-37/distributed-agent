"""Deliver model-selected project files without exposing device credentials to Codex."""

import base64
import ctypes
import hashlib
import json
import os
import stat
import urllib.error
from contextlib import ExitStack
from pathlib import Path

from agent_harness.attachments import AGENT_ATTACHMENT_FEATURE, MAX_ATTACHMENTS, MAX_FILE_BYTES
from agent_harness.common import read_json, write_json


UNSUPPORTED_READER = "此接收端暂不支持 Agent 自动发送附件；请在桌面端手动上传文件"
APPROVAL_FEATURE = "agent_attachment_approval_v1"


def project_files_supported():
    """Fail closed unless an opened file handle can be confined to the workspace."""
    if os.name == "nt":
        return True
    return (
        os.name != "nt" and os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY")
    )


def read_project_file(root, requested):
    if not project_files_supported():
        raise ValueError(UNSUPPORTED_READER)
    root = Path(root).resolve(strict=True)
    if not isinstance(requested, str) or not requested or len(requested) > 4096:
        raise ValueError("附件路径无效")
    path = Path(requested)
    try:
        relative = path.relative_to(root) if path.is_absolute() else path
    except ValueError:
        raise ValueError("附件必须位于当前项目目录") from None
    if not relative.parts or any(part.startswith(".") for part in relative.parts):
        raise ValueError("不能发送隐藏文件、内部配置或越界路径")
    if os.name == "nt":
        return _read_windows_file(root, relative)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with ExitStack() as stack:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        stack.callback(os.close, directory)
        for part in relative.parts[:-1]:
            directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            stack.callback(os.close, directory)
        descriptor = os.open(relative.name, flags, dir_fd=directory)
        stream = stack.enter_context(os.fdopen(descriptor, "rb"))
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("只能发送普通文件")
        if before.st_size > MAX_FILE_BYTES:
            raise ValueError("每个附件最大 5 MB")
        content = stream.read(MAX_FILE_BYTES + 1)
        after = os.fstat(stream.fileno())
        if len(content) > MAX_FILE_BYTES or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError("附件在读取时发生变化，请重新发送")
    return relative.name, content


def _read_windows_file(root, relative):
    """Open first, then validate the handle's actual target to avoid path swap races."""
    import msvcrt

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    kernel.CreateFileW.restype = ctypes.c_void_p
    kernel.GetFinalPathNameByHandleW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32]
    kernel.GetFinalPathNameByHandleW.restype = ctypes.c_uint32
    kernel.GetFileInformationByHandle.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel.GetFileInformationByHandle.restype = ctypes.c_int
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int

    class FileInfo(ctypes.Structure):
        _pack_ = 4
        _fields_ = [
            ("attributes", ctypes.c_uint32),
            ("created", ctypes.c_uint64),
            ("accessed", ctypes.c_uint64),
            ("written", ctypes.c_uint64),
            ("volume", ctypes.c_uint32),
            ("size_high", ctypes.c_uint32),
            ("size_low", ctypes.c_uint32),
            ("links", ctypes.c_uint32),
            ("index_high", ctypes.c_uint32),
            ("index_low", ctypes.c_uint32),
        ]

    target = root / relative
    handle = kernel.CreateFileW(str(target), 0x80000000, 0x00000001 | 0x00000002, None, 3, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise OSError(ctypes.get_last_error(), "Cannot open attachment")
    try:
        info = FileInfo()
        if not kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise OSError(ctypes.get_last_error(), "Cannot inspect attachment")
        if info.attributes & (0x10 | 0x400):  # directory or reparse point
            raise ValueError("只能发送项目目录中的普通文件")
        if ((info.size_high << 32) | info.size_low) > MAX_FILE_BYTES:
            raise ValueError("每个附件最大 5 MB")
        buffer = ctypes.create_unicode_buffer(32768)
        length = kernel.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
        if not length or length >= len(buffer):
            raise OSError(ctypes.get_last_error(), "Cannot resolve attachment")
        actual = buffer.value
        if actual.startswith("\\\\?\\UNC\\"):
            actual = "\\\\" + actual[8:]
        elif actual.startswith("\\\\?\\"):
            actual = actual[4:]
        expected_root = os.path.normcase(os.path.normpath(str(root)))
        actual = os.path.normcase(os.path.normpath(actual))
        try:
            inside = os.path.commonpath((expected_root, actual)) == expected_root
        except ValueError:
            inside = False
        if not inside:
            raise ValueError("附件必须位于当前项目目录")
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY)
        handle = None
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("只能发送普通文件")
            content = stream.read(MAX_FILE_BYTES + 1)
            after = os.fstat(stream.fileno())
            if len(content) > MAX_FILE_BYTES or (before.st_size, before.st_mtime_ns) != (
                after.st_size,
                after.st_mtime_ns,
            ):
                raise ValueError("附件在读取时发生变化，请重新发送")
        return relative.name, content
    finally:
        if handle is not None:
            kernel.CloseHandle(handle)


def _explicit_human_request(config, requested):
    """Only a current-conversation human request can take the automatic path."""
    context = config
    try:
        contract = json.loads(config.get("prompt", "{}"))
        context = json.loads(contract.get("objective", "{}"))
    except (ValueError, TypeError, AttributeError):
        pass
    messages = context.get("visible_messages", [])
    if not isinstance(messages, list):
        return False
    task = context.get("task", {})
    partner = task.get("reply_to") if task.get("private_turn") else None
    humans = [
        message
        for message in messages
        if isinstance(message, dict)
        and message.get("sender_type") == "human"
        and (message.get("sender_id") == partner if partner else message.get("recipient_id") is None)
    ]
    if not humans:
        return False
    body = str(humans[-1].get("body", "")).casefold()
    if not any(word in body for word in ("附件", "发送", "发给", "传给", "attach", "send")):
        return False
    return all(
        isinstance(item, dict)
        and isinstance(item.get("path"), str)
        and (
            item["path"].casefold() in body
            or (len(Path(item["path"]).parts) == 1 and Path(item["path"]).name.casefold() in body)
        )
        for item in requested
    )


def prepare_reply(connector, receipt, features, decision=None):
    """Upload once per slot, retry network failures, and strip local paths before reporting."""
    if receipt.get("attachment_delivery_complete"):
        return
    result, job = receipt.get("result", {}), receipt["job"]
    if job["kind"] != "task" or result.get("status") != "needs_review":
        return
    try:
        answer = json.loads(result.get("report", ""))
    except (ValueError, TypeError):
        return
    if not isinstance(answer, dict) or not isinstance(answer.get("reply", ""), str):
        return
    requested = receipt.get("attachment_requested", answer.get("attachments"))
    if not requested:
        return
    errors = []
    path = connector.receipt_path(str(job.get("job_id", job.get("id"))))
    if not isinstance(requested, list) or len(requested) > MAX_ATTACHMENTS:
        errors.append("每条回复最多 5 个附件")
        requested = []
    if AGENT_ATTACHMENT_FEATURE not in features:
        errors.append("托管端需要升级以支持 Agent 附件")
        requested = []
    if requested and not project_files_supported():
        errors.append(UNSUPPORTED_READER)
        requested = []
    config = read_json(connector.root / "jobs" / receipt["local_id"] / "request.json", {})
    root = config.get("project", {}).get("remote_path")
    binding = connector.binding_for(job) if requested and not errors else None
    policy = binding.get("attachment_policy", "ask") if binding else "ask"
    if policy not in {"ask", "auto", "full"}:
        policy = "ask"
    approval = receipt.get("attachment_approval")
    approved_contents = {}
    if requested and policy != "full" and (policy == "ask" or not _explicit_human_request(config, requested)):
        if APPROVAL_FEATURE not in features:
            errors.append("协作服务需要升级以支持附件发送批准")
            requested = []
    if (
        requested
        and not errors
        and policy != "full"
        and (policy == "ask" or not _explicit_human_request(config, requested))
    ):
        if not approval:
            manifest = []
            for slot, item in enumerate(requested):
                try:
                    if not root or not isinstance(item, dict) or set(item) != {"path"}:
                        raise ValueError("Invalid attachment descriptor")
                    name, content = read_project_file(root, item["path"])
                    relative_path = Path(item["path"])
                    if relative_path.is_absolute():
                        relative_path = relative_path.relative_to(Path(root).resolve(strict=True))
                    manifest.append(
                        {
                            "slot": slot,
                            "path": str(relative_path),
                            "name": name,
                            "size": len(content),
                            "sha256": hashlib.sha256(content).hexdigest(),
                        }
                    )
                except (OSError, ValueError):
                    errors.append(f"附件 {slot + 1} 无法读取或不在项目目录")
            if manifest:
                receipt["attachment_requested"] = requested
                pending = connector.transport(
                    connector.config,
                    "task-attachment-request",
                    {
                        "job_id": job.get("job_id", job.get("id")),
                        "lease_id": job["lease_id"],
                        "manifest": manifest,
                    },
                )
                receipt["attachment_approval"] = dict(pending, manifest=manifest)
                receipt["attachment_errors"] = errors
                receipt["phase"] = "waiting_attachment_approval"
                write_json(path, receipt)
                return
        elif decision is None:
            return
        else:
            if decision.get("id") != approval.get("id") or decision.get("digest") != approval.get("digest"):
                raise PermissionError("Attachment approval does not match this request")
            errors.extend(receipt.get("attachment_errors", []))
            if not decision.get("approved"):
                errors.append("机器所有者拒绝了附件发送")
                requested = []
            else:
                expected = {item["slot"]: item for item in approval["manifest"]}
                for slot, item in enumerate(requested):
                    if slot not in expected:
                        continue
                    try:
                        name, content = read_project_file(root, item["path"])
                    except (OSError, ValueError):
                        errors.append("批准后文件已无法读取，请重新请求")
                        requested = []
                        break
                    if (name, len(content), hashlib.sha256(content).hexdigest()) != (
                        expected[slot]["name"],
                        expected[slot]["size"],
                        expected[slot]["sha256"],
                    ):
                        errors.append("批准后文件内容发生变化，请重新请求")
                        requested = []
                        break
                    approved_contents[slot] = (name, content)
                if requested:
                    requested = [item if slot in expected else {} for slot, item in enumerate(requested)]
    uploads = receipt.setdefault("attachment_uploads", {})
    ids = []
    for slot, item in enumerate(requested):
        if str(slot) in uploads:
            ids.append(uploads[str(slot)])
            continue
        try:
            if not root or not isinstance(item, dict) or set(item) != {"path"}:
                raise ValueError("附件描述无效")
            name, content = approved_contents.get(slot) or read_project_file(root, item["path"])
        except (OSError, ValueError):
            errors.append(f"附件 {slot + 1} 无法读取：仅支持项目内的普通文件，每个最大 5 MB")
            continue
        try:
            metadata = connector.transport(
                connector.config,
                "task-attachment",
                {
                    "job_id": job.get("job_id", job.get("id")),
                    "lease_id": job["lease_id"],
                    "slot": slot,
                    "name": name,
                    "base64": base64.b64encode(content).decode("ascii"),
                },
            )
        except urllib.error.HTTPError as exc:
            if exc.code >= 500 or exc.code in {408, 429}:
                raise
            errors.append(f"附件 {slot + 1} 被协作服务拒绝（HTTP {exc.code}）")
            continue
        except (ValueError, PermissionError):
            errors.append(f"附件 {slot + 1} 未通过服务验证")
            continue
        uploads[str(slot)] = metadata["id"]
        ids.append(metadata["id"])
        write_json(path, receipt)
    answer["attachment_ids"] = ids
    answer.pop("attachments", None)
    if ids and not answer.get("reply", "").strip():
        answer["reply"] = "已发送附件。"
    if errors:
        answer["reply"] = (answer.get("reply", "") + "\n\n附件发送提示：" + "；".join(errors)).strip()[-16000:]
    result["report"] = json.dumps(answer, ensure_ascii=False)
    receipt["attachment_delivery_complete"] = True
    receipt["phase"] = "terminal"
    write_json(path, receipt)
